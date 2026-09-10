"""odibi_anchor.codebase.workflow_gate_context — Obligation tracker.

Tracks actions taken vs obligations owed during a coding session.
Reports unpaid debts with aggressive MUST/SHOULD nudges. Provides the
feedback loop that closes the gap between "agent should run tests" and
"agent actually runs tests."

Usage:
    from odibi_anchor.codebase import workflow_gate_context

    ctx = workflow_gate_context(
        "/path/to/project",
        actions_taken=["implement", "implement"],
        files_changed=["src/foo.py", "src/bar.py"],
    )
    # ctx["obligations"] → MUST run test_focus_context, consistency_check_context
    # ctx["metrics"]["risk_level"] → "high"

Dependencies: stdlib only (os, pathlib, re).
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any
from odibi_anchor._utils.contract import validate_output_format
from odibi_anchor._utils._session_state import _SESSION_TIMINGS
from odibi_anchor._utils.render_utils import render_header_lines, render_metrics_lines, render_bullet_section


# ---------------------------------------------------------------------------
# Obligation Rules
# ---------------------------------------------------------------------------

# Each rule: (action_pattern, file_pattern, priority, tool, reason_template)
# action_pattern: regex matched against action strings
# file_pattern: regex matched against changed file paths (None = no file check)
# priority: "MUST" or "SHOULD"
# tool: name of the tool that pays this obligation
# reason_template: uses {files}, {actions}, {count} placeholders

_OBLIGATION_RULES: list[dict[str, Any]] = [
    # --- Source file modifications ---
    {
        "action_pattern": r"^(implement|modify|refactor|fix|edit|update|write|add|change|delete|remove)$",
        "file_pattern": r"^src/.*\.py$",
        "priority": "MUST",
        "tool": "test_focus_context",
        "reason_template": "Modified {count} source file(s) — tests not run",
        "suggested_call_template": "test_focus_context(root, changed_files={files})",
        "dedupe_key": "test",
    },
    # --- New function/constant added ---
    {
        "action_pattern": r"^(implement|add|create)$",
        "file_pattern": r"^src/.*\.py$",
        "priority": "MUST",
        "tool": "consistency_check_context",
        "reason_template": "Added/modified code — verify conventions",
        "suggested_call_template": "consistency_check_context(root, changed_files={files})",
        "dedupe_key": "consistency_check",
    },
    # --- __init__.py modified ---
    {
        "action_pattern": r"^(implement|modify|refactor|edit|update|add|change)$",
        "file_pattern": r"__init__\.py$",
        "priority": "MUST",
        "tool": "consistency_check_context",
        "reason_template": "Modified __init__.py — exports may have drifted",
        "suggested_call_template": "consistency_check_context(root, changed_files={files})",
        "dedupe_key": "consistency_check",
    },
    # --- Changed tool output behavior ---
    {
        "action_pattern": r"^(implement|modify|refactor|change)$",
        "file_pattern": r"_context\.py$",
        "priority": "SHOULD",
        "tool": "dogfood_regression_context",
        "reason_template": "Changed tool output behavior — check for regressions",
        "suggested_call_template": "dogfood_regression_context(output, root=root)",
        "dedupe_key": "dogfood",
        "skip_for_new_files": True,
        "skip_when_no_baseline": True,
    },
    # --- Shared/imported function changed ---
    {
        "action_pattern": r"^(modify|refactor|change|fix)$",
        "file_pattern": r"^src/.*/(utils|helpers|common|shared|base).*\.py$",
        "priority": "MUST",
        "tool": "change_impact_context",
        "reason_template": "Modified shared/utility code — downstream impact unknown",
        "suggested_call_template": "change_impact_context(root, target='{first_file}', change_type='modify')",
        "dedupe_key": "change_impact",
        "skip_for_new_files": True,
    },
    # --- Data write to silver/gold ---
    {
        "action_pattern": r"^(write|persist|save|merge|upsert|insert)$",
        "file_pattern": None,
        "priority": "MUST",
        "tool": "quality_gate_context",
        "reason_template": "Data write detected — pre-write quality gate required",
        "suggested_call_template": "quality_gate_context(df, table_name, spark)",
        "dedupe_key": "quality_gate",
    },
    # --- Modified transform logic ---
    {
        "action_pattern": r"^(implement|modify|refactor|change)$",
        "file_pattern": r"(transform|pipeline|etl|process)",
        "priority": "SHOULD",
        "tool": "diff_tables_by_key",
        "reason_template": "Modified transform logic — verify output unchanged",
        "suggested_call_template": "diff_tables_by_key(before_df, after_df, keys=[...])",
        "dedupe_key": "diff_tables",
        "skip_for_new_files": True,
    },
    # --- Fixed a test ---
    {
        "action_pattern": r"^(fix|modify|update)$",
        "file_pattern": r"^tests/.*\.py$",
        "priority": "SHOULD",
        "tool": "test",
        "reason_template": "Fixed test file(s) — re-run to confirm",
        "suggested_call_template": "anchor('test', target='{first_file}')",
        "dedupe_key": "pytest_rerun",
    },
    # --- Preflight required for any Python file modification (audit-fix v0.4.0) ---
    {
        "action_pattern": r"^(implement|modify|refactor|fix|edit|update|write|add|change|delete|remove|create)$",
        "file_pattern": r"\.py$",
        "priority": "MUST",
        "tool": "preflight_context",
        "reason_template": "Modified {count} Python file(s) — preflight lint/syntax check required",
        "suggested_call_template": "preflight_context(root, changed_files={files})",
        "dedupe_key": "preflight_required",
    },
    # --- Known-bad check advised for source Python modifications (not tests) ---
    {
        "action_pattern": r"^(implement|modify|refactor|fix|edit|update|write|add|change)$",
        "file_pattern": r"^src/.*\.py$",
        "priority": "SHOULD",
        "tool": "known_bad_change_context",
        "reason_template": "Modified source file(s) — check against historical failure patterns",
        "suggested_call_template": "known_bad_change_context(root, changed_files={files})",
        "dedupe_key": "known_bad_check",
    },
    # --- Codebase map advised when modifying code (v0.4.2) ---
    {
        "action_pattern": r"^(implement|modify|refactor|fix|edit|update|write|add|change|create|delete|remove)$",
        "file_pattern": r"\.(py|sql|scala)$",
        "priority": "SHOULD",
        "tool": "codebase_map_context",
        "reason_template": "Modified {count} source file(s) — understand structure before/after changes",
        "suggested_call_template": "codebase_map_context(root)",
        "dedupe_key": "map_context",
        "skip_when_already_run": True,
    },
]

# Session-level obligations (not tied to file changes)
_SESSION_OBLIGATIONS: list[dict[str, Any]] = [
    {
        "trigger": "session_start",
        "priority": "MUST",
        "tool": "codebase_map_context",
        "reason": "First action of session on code project — orient first",
        "suggested_call": "codebase_map_context(root)",
    },
    {
        "trigger": "session_end",
        "priority": "MUST",
        "tool": "session_snapshot_context",
        "reason": "End of session — save state for next session",
        "suggested_call": "session_snapshot_context(root, decisions=[...], next_steps=[...])",
    },
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def workflow_gate_context(
    root: str | Path,
    *,
    actions_taken: list[str],
    files_changed: list[str] | None = None,
    files_created: list[str] | None = None,
    files_read: list[str] | None = None,
    workflow: str | None = None,
    obligations_paid: list[str] | None = None,
    verification_record: list[dict[str, str]] | None = None,
    skip_timing_verification: bool = False,
    _timings_override: list[dict] | None = None,
    subject: str | None = None,
    output_format: str = "dict",
) -> dict[str, Any] | str:
    """Track actions taken vs obligations owed in a coding session.

    Analyzes what actions have been performed and what verification debts
    remain unpaid. Returns MUST obligations (blocking) and SHOULD
    obligations (advisory) with exact tool calls to pay them.

    Args:
        root: Project root directory.
        actions_taken: List of action strings describing what was done.
            Examples: "implement", "modify", "write", "fix", "read",
            "explore", "test", "verify", "session_start", "session_end".
        files_changed: List of changed file paths (relative to root).
        files_created: List of newly created file paths (relative to root).
            Files in this list are excluded from obligations that only apply
            to modifications (e.g., diff_tables_by_key — no "before" to compare).
        files_read: List of files read (for context, not obligations).
        workflow: Explicit workflow type. Auto-detected if None.
            Values: "add_tool", "modify_code", "data_pipeline",
            "debug", "explore", "refactor".
        obligations_paid: List of tool names already run this session
            (e.g., ["test_focus_context", "consistency_check_context"]).
        verification_record: List of verification evidence dicts. Each dict has:
            - "obligation": str — the tool/check name (e.g., "test_focus_context")
            - "status": str — "passed", "failed", or "skipped"
            - "evidence": str — result summary (e.g., "557 passed, 0 failed")
            - "timestamp": str (optional) — when the check was run
            When provided, matching obligations are marked as paid with evidence.
            This closes the loop between "what should be verified" and "what was verified."
        subject: Human label. Defaults to directory name.
        output_format: "dict" or "markdown".

    Returns:
        Structured context dict (or markdown string).

    Example:
        >>> ctx = workflow_gate_context(
        ...     "/path/to/project",
        ...     actions_taken=["implement", "implement"],
        ...     files_changed=["src/foo.py", "src/bar.py"],
        ... )
        >>> ctx["metrics"]["risk_level"]
        'high'
        >>> ctx["obligations"][0]["tool"]
        'test_focus_context'
    """
    validate_output_format(output_format)

    root = Path(root).resolve()
    subject = subject or root.name
    files_changed = files_changed or []
    files_created = files_created or []
    files_read = files_read or []
    obligations_paid_set = set(obligations_paid or [])

    # Process verification_record into obligations_paid + evidence
    verification_record = verification_record or []
    verified_tools: dict[str, dict[str, str]] = {}
    for record in verification_record:
        tool = record.get("obligation", "")
        if tool:
            obligations_paid_set.add(tool)
            verified_tools[tool] = {
                "status": record.get("status", "unknown"),
                "evidence": record.get("evidence", ""),
                "timestamp": record.get("timestamp", ""),
            }

    # Auto-detect workflow
    detected_workflow = workflow or _detect_workflow(actions_taken, files_changed)

    # Compute obligations from action/file rules
    raw_obligations = _compute_obligations(
        actions_taken, files_changed, obligations_paid_set, files_created
    )

    # Add session-level obligations
    session_obs = _compute_session_obligations(
        actions_taken, obligations_paid_set
    )
    raw_obligations.extend(session_obs)

    # Planning obligation check — code changes without planning trigger a warning
    _PLANNING_ACTIONS = {"task", "plan"}
    _CODE_ACTIONS = {
        "edit",
        "safe",
        "semantic",
        "implement",
        "modify",
        "refactor",
        "fix",
        "update",
        "write",
        "add",
        "change",
        "delete",
        "remove",
        "create",
    }

    actions_lower = {a.lower() for a in actions_taken}
    has_planning = bool(_PLANNING_ACTIONS & actions_lower)
    has_code_changes = bool(_CODE_ACTIONS & actions_lower)

    # All code changes require full planning (anchor("task")) — there is no
    # lightweight "quick" planning path. See the task-derived skill resolver.
    if has_code_changes and not has_planning:
        planning_tool = "task_execution_context"
        planning_paid = planning_tool in obligations_paid_set
        if not planning_paid:
            raw_obligations.append({
                "tool": planning_tool,
                "reason": (
                    "Code was modified without planning. Run anchor('task') first — all changes "
                    "require full planning. Then complete the normal "
                    "touched/preflight/test/gate chain."
                ),
                "priority": "MUST",
                "created_by_action": "missing_planning",
                "suggested_call": "anchor('task', 'description', goal='...', mode='implementation')",
                "dedupe_key": "planning_required",
                "paid": False,
            })

    # Deduplicate by dedupe_key
    obligations = _dedupe_obligations(raw_obligations)

    # Verify timing proof for claimed obligations
    timing_verified, timing_rejected = _verify_timing_proof(
        obligations_paid_set,
        skip_verification=skip_timing_verification,
        _timings=_timings_override,
    )

    # Remove rejected obligations from paid set (they weren't actually run)
    if timing_rejected:
        obligations_paid_set -= set(timing_rejected)
        # Re-evaluate obligations with updated paid set
        for o in raw_obligations:
            if o["tool"] in timing_rejected:
                o["paid"] = False

    # Separate unpaid from paid
    unpaid = [o for o in obligations if not o.get("paid", False)]
    paid = []
    for tool in obligations_paid_set:
        entry: dict[str, Any] = {"tool": tool, "paid_by": "explicit"}
        if tool in verified_tools:
            entry["paid_by"] = "verification_record"
            entry.update(verified_tools[tool])
        elif tool in timing_verified:
            entry["paid_by"] = "timing_verified"
        paid.append(entry)

    # Calculate risk level
    must_unpaid = [o for o in unpaid if o["priority"] == "MUST"]
    should_unpaid = [o for o in unpaid if o["priority"] == "SHOULD"]

    if must_unpaid:
        risk_level = "high"
    elif should_unpaid:
        risk_level = "medium"
    elif obligations_paid_set:
        risk_level = "low"
    else:
        risk_level = "none"

    # Build findings
    findings = []
    src_files = [f for f in files_changed if f.startswith("src/")]
    test_files = [f for f in files_changed if f.startswith("tests/")]
    if src_files:
        findings.append(f"{len(src_files)} source file(s) modified since last verification.")
    if test_files:
        findings.append(f"{len(test_files)} test file(s) modified.")
    if not files_changed and actions_taken:
        findings.append(f"{len(actions_taken)} action(s) taken (read-only or no file tracking).")
    if obligations_paid_set:
        findings.append(f"{len(obligations_paid_set)} obligation(s) already paid.")
    if timing_rejected:
        findings.append(f"{len(timing_rejected)} claimed obligation(s) rejected (no timing proof).")

    # Build risks
    risks = []
    if must_unpaid:
        risks.append(
            f"{len(must_unpaid)} MUST obligation(s) unpaid — changes could break downstream."
        )
    if should_unpaid:
        risks.append(
            f"{len(should_unpaid)} SHOULD obligation(s) outstanding — "
            f"convention drift or regressions possible."
        )
    if timing_rejected:
        risks.append(
            f"{len(timing_rejected)} obligation(s) claimed but NOT verified by session timing: "
            f"{timing_rejected}. These were rejected — run the tools or use skip_timing_verification=True."
        )
    if not actions_taken:
        risks.append("No actions recorded — tracking may be incomplete.")

    # Verification record findings/risks
    if verification_record:
        passed = sum(1 for r in verification_record if r.get("status") == "passed")
        failed = sum(1 for r in verification_record if r.get("status") == "failed")
        findings.append(f"{passed} verification(s) passed, {failed} failed.")
        if failed:
            risks.append(f"{failed} verification(s) FAILED — do not proceed.")

    # Build suggested_next_actions with MUST/SHOULD prefix
    suggested_actions = []
    _OBLIGATION_SKILL_MAP = {
        "test_focus_context": "skills/writing-tests/SKILL.md",
        "diff_tables_by_key": "skills/data-reconciliation/SKILL.md",
        "change_impact_context": "skills/code-comprehension/SKILL.md",
    }
    for o in must_unpaid:
        skill_ref = _OBLIGATION_SKILL_MAP.get(o.get("tool", ""), "")
        skill_hint = f" — see {skill_ref}" if skill_ref else ""
        suggested_actions.append(f"MUST: {o['suggested_call']}{skill_hint}")
    for o in should_unpaid:
        skill_ref = _OBLIGATION_SKILL_MAP.get(o.get("tool", ""), "")
        skill_hint = f" — see {skill_ref}" if skill_ref else ""
        suggested_actions.append(f"SHOULD: {o['suggested_call']}{skill_hint}")
    # Recovery guidance when gate fails
    if risk_level == "high":
        suggested_actions.append(
            "RECOVERY: Gate failed. Fix all MUST obligations before re-running gate."
        )
    if not unpaid and actions_taken:
        suggested_actions.append("All obligations paid — safe to continue.")

    # Durable project decisions belong in managed authority artifacts; learning
    # captures only supported reusable observations at closure.

    # Suggest mid-session checkpoint when obligations are clear
    if not must_unpaid and files_changed and obligations_paid_set:
        suggested_actions.append(
            "SHOULD: Run anchor('snapshot', decisions=[...], next_steps=[...]) — "
            "checkpoint progress in case session dies"
        )
    if not unpaid and actions_taken:
        if files_changed:
            # BLOCKING learn reminder when session has actual changes
            suggested_actions.append(
                "BLOCKING: Close the generated obligation with explicit structured assessment "
                "(real Observation IDs or nothing_reusable_learned)."
            )
            suggested_actions.append(
                "Use anchor('learning', 'capture', ...) for genuine observations, then "
                "anchor('learning', 'assess', ...) to close the canonical lifecycle."
            )
        else:
            suggested_actions.append(
                "MUST: Close post-gate debt with anchor('learning', 'assess', ...)."
            )

    # Metrics
    metrics = {
        "actions_count": len(actions_taken),
        "timing_verified": timing_verified,
        "timing_rejected": timing_rejected,
        "files_changed_count": len(files_changed),
        "files_created_count": len(files_created),
        "obligations_owed": len(unpaid),
        "obligations_paid": len(paid),
        "must_unpaid": len(must_unpaid),
        "should_unpaid": len(should_unpaid),
        "risk_level": risk_level,
        "workflow": detected_workflow,
        "verifications_recorded": len(verification_record),
        "verifications_passed": sum(
            1 for r in verification_record if r.get("status") == "passed"
        ),
        "verifications_failed": sum(
            1 for r in verification_record if r.get("status") == "failed"
        ),
        "all_verified": (
            len(must_unpaid) == 0
            and len(verification_record) > 0
            and all(r.get("status") == "passed" for r in verification_record)
        ),
    }

    # Summary
    if unpaid:
        summary = (
            f"{len(unpaid)} UNPAID obligation(s). "
            f"Modified {len(files_changed)} file(s) "
            f"without {'running tests' if must_unpaid else 'full verification'}."
        )
    elif obligations_paid_set:
        summary = (
            f"All obligations paid ({len(paid)} verified). "
            f"Session is clean."
        )
    else:
        summary = "No obligations created (read-only session or no file changes)."

    ctx: dict[str, Any] = {
        "kind": "workflow_gate_context",
        "subject": subject,
        "summary": summary,
        "metrics": metrics,
        "obligations": [
            {
                "tool": o["tool"],
                "reason": o["reason"],
                "priority": o["priority"],
                "created_by_action": o.get("created_by_action", ""),
                "suggested_call": o["suggested_call"],
            }
            for o in unpaid
        ],
        "obligations_paid": paid,
        "verification_record": [
            {
                "obligation": r.get("obligation", ""),
                "status": r.get("status", "unknown"),
                "evidence": r.get("evidence", ""),
                "timestamp": r.get("timestamp", ""),
            }
            for r in verification_record
        ],
        "findings": findings,
        "risks": risks,
        "samples": {},
        "suggested_next_actions": suggested_actions,
        "learn_reminder": _build_learn_reminder(
            risk_level, files_changed, actions_taken, timing_rejected
        ) if not unpaid and files_changed else None,
    }

    if output_format == "markdown":
        return render_workflow_gate_report(ctx)
    return ctx


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------


def render_workflow_gate_report(ctx: dict[str, Any]) -> str:
    """Render workflow_gate_context output as markdown."""
    lines = render_header_lines(ctx, "Workflow Gate")
    lines.extend(render_metrics_lines(ctx["metrics"]))

    if ctx["obligations"]:
        lines.extend(["", "## Unpaid Obligations", ""])
        for o in ctx["obligations"]:
            lines.append(
                f"- **{o['priority']}** `{o['tool']}` — {o['reason']}"
            )
            lines.append(f"  - Call: `{o['suggested_call']}`")

    if ctx["obligations_paid"]:
        lines.extend(["", "## Obligations Paid", ""])
        for p in ctx["obligations_paid"]:
            lines.append(f"- ✅ `{p['tool']}`")

    # Verification Record
    records = ctx.get("verification_record", [])
    if records:
        lines.extend(["", "## Verification Record", ""])
        lines.append("| Obligation | Status | Evidence |")
        lines.append("| --- | --- | --- |")
        for r in records:
            status_icon = "\u2705" if r["status"] == "passed" else "\u274c" if r["status"] == "failed" else "\u23ed\ufe0f"
            lines.append(f"| {r['obligation']} | {status_icon} {r['status']} | {r['evidence']} |")

    lines.extend(render_bullet_section(ctx["findings"], "## Findings"))
    lines.extend(render_bullet_section(ctx["risks"], "## Risks"))
    lines.extend(render_bullet_section(ctx["suggested_next_actions"], "## Next Actions"))

    return "\n".join(lines)



# ---------------------------------------------------------------------------
# Timing Verification (v0.5.1)
# ---------------------------------------------------------------------------

# Maps obligation tool names → dispatcher action keys that satisfy them
_OBLIGATION_TO_TIMING_KEY: dict[str, list[str]] = {
    "preflight_context": ["preflight", "safe"],  # safe auto-chains preflight
    "test_focus_context": ["test", "safe"],       # safe auto-chains test_focus
    "consistency_check_context": ["consistency"],
    "codebase_map_context": ["map"],
    "change_impact_context": ["impact"],
    "known_bad_change_context": ["known_bad", "safe"],  # safe auto-chains known_bad
    "diff_tables_by_key": ["diff"],
    "dogfood_regression_context": ["dogfood"],
    "quality_gate_context": ["quality"],
    "session_snapshot_context": ["snapshot"],
    "task_execution_context": ["task"],   # satisfies planning
}

# Obligations that cannot be verified via timing (external tools, manual checks)
_TIMING_EXEMPT_OBLIGATIONS: set[str] = {
    "pytest",  # external test runner — not a anchor() action
}


def _verify_timing_proof(
    obligations_paid_set: set[str],
    *,
    skip_verification: bool = False,
    _timings: list[dict] | None = None,
) -> tuple[list[str], list[str]]:
    """Cross-reference claimed obligations against session timing records.

    Args:
        obligations_paid_set: Set of obligation tool names claimed as paid.
        skip_verification: If True, accept all obligations without checking.
        _timings: Override for _SESSION_TIMINGS (testing only).

    Returns:
        (verified, rejected) — lists of obligation tool names.
        Verified: timing proof exists (action was actually run).
        Rejected: no timing proof found (claimed but not run).
    """
    if skip_verification:
        return list(obligations_paid_set), []

    # Build set of actions actually run this session
    timings_source = _timings if _timings is not None else _SESSION_TIMINGS
    actions_run = {t["action"] for t in timings_source if t.get("error") is None}

    verified: list[str] = []
    rejected: list[str] = []

    for obligation in obligations_paid_set:
        # Exempt obligations (external tools)
        if obligation in _TIMING_EXEMPT_OBLIGATIONS:
            verified.append(obligation)
            continue

        # Check if any satisfying action was run
        satisfying_keys = _OBLIGATION_TO_TIMING_KEY.get(obligation)
        if satisfying_keys is None:
            # Unknown obligation — can't verify, allow (forward compatibility)
            verified.append(obligation)
            continue

        if any(key in actions_run for key in satisfying_keys):
            verified.append(obligation)
        else:
            rejected.append(obligation)

    return verified, rejected




def _build_learn_reminder(
    risk_level: str,
    files_changed: list[str],
    actions_taken: list[str],
    timing_rejected: list[str],
) -> dict[str, Any]:
    """Build a canonical structured-learning reminder from observed session facts."""
    events: list[dict[str, str]] = []

    # Always suggest a decision event for sessions with changes
    events.append({
        "type": "decision",
        "content": f"Modified {len(files_changed)} file(s): {', '.join(files_changed[:5])}"
        + (f" (+{len(files_changed)-5} more)" if len(files_changed) > 5 else ""),
    })

    # If there were timing rejections, note the compliance gap
    if timing_rejected:
        events.append({
            "type": "gotcha",
            "content": f"Compliance gap: {timing_rejected} claimed without timing proof. "
            "Run tools before claiming them as paid.",
        })

    return {
        "status": "BLOCKING" if files_changed else "SHOULD",
        "message": "Gate passed; canonical structured learning assessment is required.",
        "suggested_call": (
            "anchor('learning', 'capture', ...) for evidence-backed candidates, then "
            "anchor('learning', 'assess', outcome='observations_recorded', observation_ids=[...]); "
            "use outcome='nothing_reusable_learned' only when no reusable observation exists"
        ),
        "event_templates": events,
    }

# ---------------------------------------------------------------------------
# Workflow Detection
# ---------------------------------------------------------------------------


def _detect_workflow(
    actions: list[str], files_changed: list[str]
) -> str:
    """Auto-detect workflow from actions and file patterns."""
    action_set = set(a.lower() for a in actions)
    files_lower = [f.lower() for f in files_changed]

    # Check for data pipeline indicators
    data_actions = {"write", "persist", "save", "merge", "upsert", "insert"}
    if action_set & data_actions:
        return "data_pipeline"

    # Check for debug indicators
    debug_actions = {"debug", "diagnose", "investigate", "trace"}
    if action_set & debug_actions:
        return "debug"

    # Check for exploration
    explore_actions = {"read", "explore", "profile", "inspect", "describe"}
    if action_set <= explore_actions:
        return "explore"

    # Check file patterns for code work
    has_src = any(f.startswith("src/") for f in files_lower)
    has_tests = any(f.startswith("tests/") for f in files_lower)
    has_init = any("__init__" in f for f in files_lower)

    if has_src and has_tests and has_init:
        return "add_tool"
    elif has_src and has_tests:
        return "modify_code"
    elif has_src:
        # Check if creating new files vs modifying
        create_actions = {"implement", "add", "create"}
        if action_set & create_actions:
            return "add_tool"
        return "modify_code"

    # Check for refactor
    refactor_actions = {"refactor", "rename", "move", "restructure"}
    if action_set & refactor_actions:
        return "refactor"

    return "modify_code"  # safe default


# ---------------------------------------------------------------------------
# Obligation Computation
# ---------------------------------------------------------------------------


def _compute_obligations(
    actions: list[str],
    files_changed: list[str],
    paid: set[str],
    files_created: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Compute obligations from action/file rule table."""
    obligations: list[dict[str, Any]] = []
    created_set = set(files_created or [])

    for rule in _OBLIGATION_RULES:
        action_re = re.compile(rule["action_pattern"], re.IGNORECASE)
        file_re = (
            re.compile(rule["file_pattern"], re.IGNORECASE)
            if rule["file_pattern"]
            else None
        )

        # Check if any action matches
        matching_actions = [a for a in actions if action_re.match(a)]
        if not matching_actions:
            continue

        # Check if any file matches (if file pattern specified)
        if file_re:
            matching_files = [f for f in files_changed if file_re.search(f)]
            if not matching_files:
                continue
        else:
            matching_files = files_changed

        # Skip rules marked skip_for_new_files if ALL matching files are new
        if rule.get("skip_for_new_files") and matching_files:
            if all(f in created_set for f in matching_files):
                continue

        # Skip dogfood obligation if no baselines exist for the specific tools modified
        if rule.get("skip_when_no_baseline") and matching_files:
            baseline_dir = os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                "..", ".dogfood_baselines"
            )
            if not os.path.isdir(baseline_dir):
                continue
            baseline_names = os.listdir(baseline_dir) if os.path.isdir(baseline_dir) else []
            # Check if ANY modified file has a corresponding baseline
            has_relevant_baseline = False
            for mf in matching_files:
                # Extract tool stem: "src/.../workflow_gate_context.py" → "workflow_gate_context"
                stem = os.path.basename(mf).replace(".py", "")
                if any(bn.startswith(stem) for bn in baseline_names):
                    has_relevant_baseline = True
                    break
            if not has_relevant_baseline:
                continue

        # Skip obligation if tool was already run this session (timing proof exists)
        if rule.get("skip_when_already_run"):
            from odibi_anchor._utils._session_state import _SESSION_TIMINGS
            tool_name = rule["tool"]
            # Match variants: "codebase_map_context" → also check "codebase_map", "map"
            action_variants = {tool_name, tool_name.replace("_context", "")}
            # Dispatcher often uses short name (e.g., "map" for "codebase_map_context")
            short = tool_name.replace("_context", "").split("_", 1)[-1] if "_" in tool_name else tool_name
            action_variants.add(short)
            already_ran = any(
                t["action"] in action_variants
                for t in _SESSION_TIMINGS
                if t.get("error") is None
            )
            if already_ran:
                continue

        # Build obligation
        files_repr = repr(matching_files) if matching_files else "[]"
        first_file = matching_files[0] if matching_files else ""
        files_space = " ".join(matching_files) if matching_files else ""

        reason = rule["reason_template"].format(
            files=files_repr,
            count=len(matching_files),
            actions=len(matching_actions),
        )
        suggested_call = rule["suggested_call_template"].format(
            files=files_repr,
            first_file=first_file,
            files_space_separated=files_space,
        )

        is_paid = rule["tool"] in paid

        obligations.append({
            "tool": rule["tool"],
            "reason": reason,
            "priority": rule["priority"],
            "created_by_action": matching_actions[0],
            "suggested_call": suggested_call,
            "dedupe_key": rule["dedupe_key"],
            "paid": is_paid,
        })

    return obligations


def _compute_session_obligations(
    actions: list[str],
    paid: set[str],
) -> list[dict[str, Any]]:
    """Compute session-level obligations."""
    obligations: list[dict[str, Any]] = []
    action_set = set(a.lower() for a in actions)

    for rule in _SESSION_OBLIGATIONS:
        trigger = rule["trigger"]
        if trigger in action_set:
            is_paid = rule["tool"] in paid
            obligations.append({
                "tool": rule["tool"],
                "reason": rule["reason"],
                "priority": rule["priority"],
                "created_by_action": trigger,
                "suggested_call": rule["suggested_call"],
                "dedupe_key": f"session_{rule['tool']}",
                "paid": is_paid,
            })

    return obligations


def _dedupe_obligations(
    obligations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Deduplicate obligations by dedupe_key, keeping highest priority."""
    seen: dict[str, dict[str, Any]] = {}

    for o in obligations:
        key = o["dedupe_key"]
        if key not in seen:
            seen[key] = o
        else:
            # Keep MUST over SHOULD
            existing = seen[key]
            if o["priority"] == "MUST" and existing["priority"] == "SHOULD":
                seen[key] = o
            # If same priority, keep the first one (more specific reason)

    return list(seen.values())
