"""Pre-dispatch enforcement checks for anchor() dispatcher."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from odibi_anchor._dispatcher._effects import (
    ActionContract,
    InvocationResolution,
    PreTaskAccess,
    enforce_effects,
    resolve_invocation,
)


@dataclass(frozen=True)
class PreTaskDecision:
    """Immutable workflow-access decision carried through one invocation."""

    pre_task: bool
    pre_task_access: PreTaskAccess
    attempt: int | None


def run_pre_dispatch_enforcement(
    action: str,
    args: tuple,
    kwargs: dict[str, Any],
    *,
    session_state: Any,
    session_timings: list[dict[str, Any]],
    session_files_changed: set[str],
    session_boot_manifest: dict[str, Any],
    planning_required_actions: frozenset[str],
    root: str,
    invocation_resolution: InvocationResolution | None = None,
    action_contract: ActionContract | None = None,
    route_binding: Any | None = None,
) -> PreTaskDecision | None:
    """Run all pre-dispatch enforcement checks; raises RuntimeError on block."""

    compatibility_call = invocation_resolution is None

    # SQLite is authoritative for post-gate debt. Keep closure and read views
    # usable after restart while preventing identity/routing replacement.
    from odibi_anchor.codebase.structured_learning_context import active_learning_obligation
    owner_project = (
        getattr(session_state, "learning_owner_project_id", None)
        or getattr(session_state, "active_project", None)
    )
    owner_task = getattr(session_state, "task_window_id", None)
    active_obligation = (
        active_learning_obligation(
            project_id=owner_project, task_window_id=owner_task,
        )
        if owner_project and owner_task
        else None
    )
    recovering_prior_task = bool(
        active_obligation
        and (
            session_state.prior_learn_debt
            or active_obligation["task_window_id"] != session_state.task_window_id
        )
    )
    recovery_actions = {"learning", "learn", "context", "prepare"}
    recovery_memory_action = (
        action == "memory"
        and bool(args)
        and str(args[0]).lower().strip() in {"disposition", "evaluate"}
    )
    if recovering_prior_task and action not in recovery_actions and not recovery_memory_action:
        raise RuntimeError(
            "BLOCKED: Workbench learning recovery is active. Only learning "
            "capture/assess/list/show/insights, read-only context/preparation, and pending "
            "memory disposition/evaluation are available until assessment closes it."
        )
    if recovering_prior_task and action == "learning":
        command = str(args[0]).lower().strip() if args else "list"
        if command not in {"capture", "assess", "list", "show", "insights", "safe_stop"}:
            raise RuntimeError(
                "BLOCKED: this learning mutation is unavailable during debt recovery."
            )
    if active_obligation and action in {"task", "new_session", "project"}:
        raise RuntimeError(
            "BLOCKED: learning recovery requires assessment closure before task, session, "
            "or project routing changes. Use anchor('learning', 'capture', ...) when needed, "
            "then anchor('learning', 'assess', ...)."
        )
    if (
        action == "learning"
        and (str(args[0]).lower().strip() if args else "list") == "assess"
        and active_obligation is not None
    ):
        from odibi_anchor._dispatcher._boot import _ENV
        from odibi_anchor.codebase._memory_lifecycle import (
            pending_task_selections,
            unevaluated_task_applications,
        )

        pending = pending_task_selections(
            _ENV["memory_db"], task_window_id=active_obligation["task_window_id"],
        )
        if pending:
            memory_ids = [item["memory_id"] for item in pending]
            raise RuntimeError(
                "BLOCKED: selected task memories require an explicit disposition before "
                f"learning assessment: {memory_ids}. Use anchor('memory', 'disposition', "
                "memory_id='...', disposition='applied|irrelevant|suspect|superseded', "
                "reason={...})."
            )
        unevaluated = unevaluated_task_applications(
            _ENV["memory_db"], task_window_id=active_obligation["task_window_id"],
        )
        if unevaluated:
            application_ids = [item["application_id"] for item in unevaluated]
            raise RuntimeError(
                "BLOCKED: applied task memories require evidence-backed evaluation before "
                f"learning assessment: {application_ids}. Use anchor('memory', 'evaluate', "
                "application_id='...', outcome='helpful|not_helpful|harmful|superseded', "
                "evidence={...})."
            )
        if kwargs.get("outcome") == "nothing_reusable_learned":
            epoch = getattr(session_state, "task_verification_epoch", None)
            observed_failures = [
                row for row in session_timings[epoch or 0:] if row.get("error")
            ]
            if observed_failures and not str(kwargs.get("notes") or "").strip():
                actions = sorted({str(row.get("action") or "unknown") for row in observed_failures})
                raise RuntimeError(
                    "BLOCKED: nothing_reusable_learned requires notes addressing observed "
                    f"failed action candidates {actions}; capture supported reusable facts or "
                    "state why these bounded failures are not reusable."
                )

    # ── Prior session learn debt: block task until debt is paid ──
    if action == "task":
        from odibi_anchor._dispatcher._enforcement import should_block_learn_debt
        blocked, msg = should_block_learn_debt(session_state.prior_learn_debt)
        if blocked:
            raise RuntimeError(
                f"BLOCKED: {msg}\n"
                "This is compatibility-only historical debt created before structured "
                "learning obligation IDs were retained.\n"
                "Historical recovery technically requires anchor(\"learn\", session_events=[{\"type\": \"decision\", "
                "\"detail\": \"what happened in the prior session\"}]) first."
            )

    # ── Config mutation guardrail ──
    if action == "config":
        from odibi_anchor._dispatcher._enforcement import should_block_config_mutation
        blocked, msg = should_block_config_mutation(session_timings, set(kwargs.keys()))
        if blocked:
            raise RuntimeError(
                f"BLOCKED: {msg}\n"
                "Config changes weaken enforcement — they need explicit planning.\n"
                "Run anchor(\"task\", goal=\"...\", mode=\"...\") first, then modify config."
            )

    # ── Enforce prerequisite sequence before task ──
    if action == "task":
        from odibi_anchor._dispatcher._enforcement import (
            should_block_task_inputs,
            should_block_task_prerequisites,
        )
        from odibi_anchor._dispatcher._protocol import STARTUP_SEQUENCE, protocol_invocation
        continuation = kwargs.get("continuation", False)
        if type(continuation) is not bool:
            raise TypeError("continuation must be a bool")
        if continuation and any(
            timing["action"] == "new_session" and timing.get("error") is None
            for timing in session_timings
        ):
            raise ValueError(
                "continuation=True replaces the inline new_session call; do not use both"
            )
        blocked, msg = should_block_task_prerequisites(
            session_timings, allow_inline_continuation=continuation,
        )
        if blocked:
            from odibi_anchor._dispatcher._blocked_action import BlockedActionError
            from odibi_anchor._dispatcher._enforcement import required_task_prerequisite
            _seq = " → ".join(protocol_invocation(step) for step in STARTUP_SEQUENCE)
            raise BlockedActionError(
                f"BLOCKED: {msg}\n"
                f"Sequence: bootstrap → {_seq}",
                required_action=required_task_prerequisite(session_timings) or STARTUP_SEQUENCE[0],
                resume_action="task",
                argument_guidance=("Complete the startup sequence in this process.",),
            )
        task_desc = args[0] if args else ""
        task_goal = kwargs.get("goal", "")
        blocked, msg = should_block_task_inputs(task_desc, task_goal)
        if blocked:
            raise RuntimeError(
                f"BLOCKED: {msg}\n"
                "Provide a clear description and goal: "
                "anchor(\"task\", \"what you are doing\", goal=\"intended outcome\", mode=\"...\")"
            )
        if continuation and not kwargs.get("acceptance_criteria"):
            raise ValueError("continuation=True requires explicit acceptance_criteria")

    # ── Data write-safety gate (shift-left, D-001) ──
    _check_data_write(action, kwargs, session_timings)

    # ── Hard planning gate ──
    if action in planning_required_actions:
        from odibi_anchor._dispatcher._enforcement import should_block_planning_gate
        blocked, msg = should_block_planning_gate(
            action,
            session_timings,
            planning_required_actions,
            task_context_present=session_state.active_task_profile is not None,
        )
        if blocked:
            raise RuntimeError(
                f"BLOCKED: {msg}\n"
                f"No exceptions — every task gets full planning."
            )

    # ── Known-bad guardrail ──
    # Planning comes first so a caller without an accepted task receives the
    # first executable prerequisite rather than circular recovery guidance.
    _check_known_bad(action, args, kwargs, session_timings)

    # Gate mode mismatch before gate wrappers advance checkpoint/file state or
    # timing persistence creates learn debt for a delivery that must be rejected.
    if action == "gate" and session_files_changed:
        from odibi_anchor._dispatcher._enforcement import should_block_mode_mismatch
        blocked, msg = should_block_mode_mismatch(session_timings, session_files_changed)
        if blocked:
            raise RuntimeError(f"BLOCKED: {msg}")

    # Validate prospective documentation paths before touched can register them.
    if action == "touched" and args:
        from odibi_anchor._dispatcher._enforcement import should_block_documentation_paths
        blocked, msg = should_block_documentation_paths(
            session_state.active_task_mode, [str(args[0])],
        )
        if blocked:
            raise RuntimeError(f"BLOCKED: {msg}")

    # Validate caller-supplied semantics only after lifecycle and authority
    # repair diagnostics have had deterministic precedence, but before a
    # covered handler can perform a persistent mutation.
    from odibi_anchor._dispatcher._action_preparation import validate_mutation_submission
    validate_mutation_submission(
        action, args, kwargs, session_state=session_state, route_binding=route_binding,
    )

    # Resolve current policy and direct skill requirements from the same accepted
    # immutable task profile. Consequential-effect enforcement below consumes this
    # policy; no mode list independently invents Spec obligations.
    active_task_profile = getattr(session_state, "active_task_profile", None)
    current_task_context = None
    current_policies = None
    if active_task_profile is not None:
        from odibi_anchor.planning._task_policy import evaluate_fresh_task_policies
        current_task_context, current_policies = evaluate_fresh_task_policies(
            active_task_profile,
            session_state=session_state,
            current_action=action,
            source_files_changed=tuple(sorted(session_files_changed)),
            current_phase=getattr(session_state, "current_phase", 1),
            phase_count=getattr(session_state, "phase_count", 1),
        )
    # ── Skill-load gate ──
    # Fires before any substantive work action once a task mode is active — not
    # just file edits — so read-only/analysis flows must load planning /
    # comprehension skills too. should_block_skill_gate self-filters via the
    # exempt set (orientation/compliance/meta machinery) and no-ops before a task.
    from odibi_anchor._dispatcher._enforcement import should_block_skill_gate
    from odibi_anchor.planning._task_builders import (
        _SKILL_GATE_EXEMPT,
        required_skills_for_task,
    )
    required_skills = (
        required_skills_for_task(
            current_task_context,
            specification_disposition=current_policies.specification.disposition,
        )
        if current_task_context is not None and current_policies is not None else ()
    )
    blocked, msg = should_block_skill_gate(
        action, session_state.active_task_mode, session_state.skills_loaded,
        {session_state.active_task_mode: required_skills}, _SKILL_GATE_EXEMPT,
    )
    if blocked:
        raise RuntimeError(
            f"BLOCKED: {msg}\n"
            f"Call anchor(\"skill_loaded\", \"<name>\") for each to receive its complete guidance."
        )

    # ── Data-spec evidence gate (S-2): persist requires proven source evidence ──
    if action == "spec" and args and str(args[0]).lower().strip() == "persist":
        from odibi_anchor._dispatcher._enforcement import should_block_spec_persist_evidence
        from odibi_anchor.planning._task_builders import _DATA_SPEC_MODES
        blocked, msg = should_block_spec_persist_evidence(
            session_state.active_task_mode, session_timings, _DATA_SPEC_MODES,
        )
        if blocked:
            raise RuntimeError(
                f"BLOCKED: {msg}\n"
                "Shift-left: a data spec built on assumptions ships bad data. "
                "Supply the missing source-evidence operations before persisting."
            )

    # ── Ungated edit limit ──
    from odibi_anchor._dispatcher._limits import lifecycle_limits
    limits = lifecycle_limits(root, session_state.active_task_profile)
    if action in ("safe", "semantic", "touched"):
        from odibi_anchor._dispatcher._enforcement import should_block_edit_limit
        max_ungated = int(limits["max_ungated_edits"])
        blocked, msg = should_block_edit_limit(session_timings, max_ungated=max_ungated)
        if blocked:
            raise RuntimeError(
                f"BLOCKED: {msg}\n"
                f"Run anchor(\"gate\") or anchor(\"checkpoint\", ...) to verify quality before continuing.\n"
                f"Maximum ungated edits: {max_ungated}."
            )

    # ── Auto-checkpoint nudge ──
    if action == "touched":
        _check_checkpoint_threshold(
            args, root, session_files_changed, session_boot_manifest, session_state, limits,
        )

    # Production passes the already-resolved invocation. The contract fallback
    # preserves focused direct callers without introducing a second resolver.
    resolution = invocation_resolution
    if resolution is None:
        if action_contract is None:
            raise ValueError("invocation_resolution or action_contract is required")
        resolution = resolve_invocation(action_contract, args, kwargs)

    task_baseline = getattr(session_state, "task_repository_baseline", None)
    if (
        resolution.error is None
        and getattr(task_baseline, "evidence_kind", None) == "databricks_git_folder"
        and (action == "touched" or "source_write" in resolution.effects)
    ):
        allow_paths = ()
        if action == "touched":
            candidate = args[0] if args else kwargs.get("target")
            allow_paths = (str(candidate),) if candidate else ()
        from odibi_anchor._repository_snapshot import verify_databricks_task_preconditions
        verify_databricks_task_preconditions(
            task_baseline,
            session_state.task_repository_write_fingerprints,
            allow_paths=allow_paths,
            incremental_acknowledgement=(action == "touched"),
        )

    # Effect checks deliberately run after established action-specific gates so
    # overlap cannot erase their more helpful diagnostics.
    if session_state.active_task_profile is not None:
        from odibi_anchor._dispatcher._effects import EFFECT_PERMISSIONS
        from odibi_anchor.planning._task_policy import evaluate_fresh_task_policies
        for effect in resolution.effects:
            context, policies = evaluate_fresh_task_policies(
                session_state.active_task_profile, session_state=session_state,
                current_action=action, current_effect=effect,
                current_effect_permissions=EFFECT_PERMISSIONS[effect],
                source_files_changed=tuple(sorted(session_files_changed)),
                checkpoint_final=bool(kwargs.get("final", False)),
                current_phase=getattr(session_state, "current_phase", 1),
                phase_count=getattr(session_state, "phase_count", 1),
            )
            # A required spec blocks consequential work, not creation/linking of
            # task/problem/spec artifacts or universally permitted bookkeeping.
            spec_exempt = action in {
                "task", "problem", "spec", "work_item", "new_session", "checkpoint", "learn",
                "skill_loaded", "log", "status", "help", "skills", "project",
            }
            has_spec = bool(
                context.linked_spec
                and context.linked_spec == getattr(session_state, "persisted_spec_name", None)
                and context.linked_spec == getattr(session_state, "reviewed_spec_name", None)
                and getattr(session_state, "spec_review_rating", None) in {"good", "excellent"}
            )
            if (policies.specification.disposition == "required" and not has_spec
                    and not spec_exempt and effect in {"artifact_write", "source_write", "data_write"}):
                raise RuntimeError(
                    "BLOCKED: task policy requires a specification before this consequential effect. "
                    "Create or link one with anchor('spec', ...) or anchor('task', spec='...')."
                )
    learning_selector = str(args[0]).strip().lower() if action == "learning" and args else "list"
    recovery_closure = bool(
        active_obligation
        and (
            action == "learn"
            or (action == "learning" and learning_selector in {"capture", "assess"})
            or recovery_memory_action
        )
    )
    reviewed_seed_load = bool(
        action == "memory"
        and (str(args[0]).strip().lower() if args else "") == "seed"
        and str(kwargs.get("command", "inspect")).strip().lower() == "load"
        and resolution.pre_task_access == "context_collection"
    )
    if not recovery_closure and not reviewed_seed_load:
        enforce_effects(action, resolution.effects, session_state.active_task_profile)

    if resolution.error is not None:
        raise RuntimeError(
            f"BLOCKED: invocation semantics could not be resolved for "
            f"{action!r}: {resolution.error}"
        ) from resolution.error

    # Gate completion intentionally makes ordinary task-required calls demand
    # fresh planning. Structured-learning closure and its task-owned memory
    # bookkeeping must remain possible instead of deadlocking behind that rule.
    if recovery_closure:
        decision = PreTaskDecision(False, resolution.pre_task_access, None)
        return None if compatibility_call else decision

    epoch = getattr(session_state, "task_verification_epoch", None)
    fresh_task = (
        getattr(session_state, "active_task_profile", None) is not None
        and epoch is not None
        and not any(
            timing["action"] in {"gate", "checkpoint"}
            and timing.get("passed") is True
            for timing in session_timings[epoch:]
        )
    )
    pre_task = not fresh_task
    if getattr(session_state, "checkpoint_in_progress", None) is not None:
        decision = PreTaskDecision(pre_task, resolution.pre_task_access, None)
        return None if compatibility_call else decision
    if fresh_task or resolution.pre_task_access != "task_required":
        decision = PreTaskDecision(pre_task, resolution.pre_task_access, None)
        return None if compatibility_call else decision

    attempt = getattr(session_state, "pre_task_task_required_attempts", 0) + 1
    session_state.pre_task_task_required_attempts = attempt
    if attempt >= 8:
        raise RuntimeError(
            "BLOCKED: 8 task-required anchor() attempts without fresh planning.\n"
            "Run anchor(\"task\", goal=\"...\", mode=\"...\") before continuing.\n"
            "Every session requires planning — no exceptions."
        )
    decision = PreTaskDecision(pre_task, resolution.pre_task_access, attempt)
    return None if compatibility_call else decision


def _check_known_bad(
    action: str,
    args: tuple,
    kwargs: dict[str, Any],
    session_timings: list[dict[str, Any]],
) -> None:
    """Require known_bad check before .py file edits."""
    kb_target = None
    if action in ("safe", "semantic"):
        kb_target = kwargs.get("target", "")
    elif action == "touched":
        kb_target = args[0] if args else ""
    if kb_target and kb_target.endswith(".py"):
        from odibi_anchor._dispatcher._enforcement import should_block_known_bad
        blocked, msg = should_block_known_bad(action, kb_target, session_timings)
        if blocked:
            raise RuntimeError(
                f"BLOCKED: {msg}\n"
                f"Check for known failure patterns before editing."
            )


def _check_data_write(
    action: str,
    kwargs: dict[str, Any],
    session_timings: list[dict[str, Any]],
) -> None:
    """Require a quality check before every valid apply_sql execution (D-001).

    Both modes execute caller SQL and are data writes. Invalid modes are left to
    effect resolution so the invalid-mode diagnostic wins.
    """
    if action != "apply_sql":
        return
    if kwargs.get("mode", "view") not in {"view", "table"}:
        return  # invalid modes are diagnosed by effect resolution
    from odibi_anchor._dispatcher._enforcement import (
        should_block_data_write_unchecked,
    )
    blocked, msg = should_block_data_write_unchecked(session_timings)
    if blocked:
        raise RuntimeError(
            f"BLOCKED: {msg}\n"
            "Shift-left: catching a bad key or schema at write time costs 1 turn; "
            "debugging corrupt downstream data costs 100.\n"
            "Load skills/data-operations/SKILL.md for the write-safety sequence."
        )


def _check_checkpoint_threshold(
    args: tuple,
    root: str,
    session_files_changed: set[str],
    session_boot_manifest: dict[str, Any],
    session_state: Any,
    limits: dict[str, int | str],
) -> None:
    """Block when too many files without checkpoint."""
    threshold = int(limits["checkpoint_file_threshold"])
    current_touch = args[0] if args else ""
    is_reconciliation = False
    is_new_touch = current_touch and current_touch not in session_files_changed
    if is_new_touch and session_boot_manifest:
        rel = current_touch.replace(os.sep, "/")
        if rel in session_boot_manifest:
            try:
                abs_path = os.path.join(root, rel)
                st = os.stat(abs_path)
                boot_entry = session_boot_manifest[rel]
                is_reconciliation = (
                    st.st_mtime != boot_entry.get("mtime")
                    or st.st_size != boot_entry.get("size")
                )
            except (OSError, PermissionError, KeyError):
                pass
    from odibi_anchor._dispatcher._enforcement import should_block_checkpoint
    blocked, msg = should_block_checkpoint(
        files_changed=session_files_changed,
        files_at_last_checkpoint=session_state.files_at_last_checkpoint,
        current_touch=current_touch,
        threshold=threshold,
        is_reconciliation=is_reconciliation,
    )
    if blocked:
        raise RuntimeError(
            f"BLOCKED: {msg}\n"
            "Run anchor(\"checkpoint\", label=\"<feature>\", "
            "learning_assessment={\"outcome\": \"nothing_reusable_learned\"}) "
            "before touching more files.\n"
            f"This prevents the 'many files changed without checkpoint between features' compliance gap.\n"
            f"Effective files per checkpoint: {threshold} "
            f"(risk={limits['checkpoint_risk']}, configured upper bound="
            f"{limits['checkpoint_configured_upper_bound']})."
        )
