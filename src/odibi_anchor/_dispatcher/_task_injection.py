"""Task context injection — extracted from agent_init.py.

Pure function that injects session state into task kwargs before planning.
Phase 2 of SPEC_DRIVEN_WORKFLOW_SPEC adds spec= handling.
"""

from __future__ import annotations

import os
import re
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, cast


def inject_session_context(
    kwargs: dict[str, Any],
    frame: Any,
    files_changed: set[str],
    learn_debt: bool,
    *,
    task_text: str = "",
    specs_dir: str | Path | None = None,
    problems_dir: str | Path | None = None,
    session_state: Any = None,
) -> dict[str, Any]:
    """Inject session state into task kwargs (append, never replace user input).

    Called by the dispatcher before _task_execution_context() to give the planner
    awareness of what has already happened this session.

    Args:
        kwargs: The user-provided kwargs to anchor("task").
        frame: ContextFrame instance (or None if frame disabled).
        files_changed: Set of files modified this session.
        learn_debt: Whether prior session learn debt is active.
        task_text: Raw positional task text used for progressive rigor guidance.
        specs_dir: Path to the specs/ directory (for spec= resolution).
        problems_dir: Path to the managed problems/ directory.
        session_state: SessionState instance used only as task-staging input.

    Returns:
        New dict with session context injected (original kwargs unchanged).
        The 'spec' key is consumed here and NOT passed through.
    """
    injected = dict(kwargs)
    continuation = injected.pop("continuation", False)
    if type(continuation) is not bool:
        raise TypeError("continuation must be a bool")

    from odibi_anchor.planning._task_profile import normalize_task_profile
    profile = normalize_task_profile(
        legacy_mode=str(injected.get("mode", "planning")),
        work_type=injected.get("work_type"), execution_mode=injected.get("execution_mode"),
        risk=injected.get("risk"), rigor=injected.get("rigor"), domains=injected.get("domains"),
        traits=injected.get("traits"), caller_required_evidence=injected.get("caller_required_evidence"),
        task_text=" ".join(filter(None, [
            task_text,
            str(injected.get("goal") or ""),
            str(injected.get("desired_outcome") or ""),
        ])),
        priority=injected.get("priority"),
    )
    repository_scope = injected.pop("repository_scope", None)
    if repository_scope is not None and (
        not isinstance(repository_scope, list)
        or not repository_scope
        or any(not isinstance(item, str) or not item.strip() for item in repository_scope)
    ):
        raise TypeError("repository_scope must be a non-empty list of relative path strings")
    if repository_scope is not None:
        absolute_entries = [
            item for item in repository_scope
            if PurePosixPath(item.replace("\\", "/")).is_absolute()
            or PureWindowsPath(item).is_absolute()
            or bool(PureWindowsPath(item).drive)
        ]
        if absolute_entries:
            raise ValueError(
                "repository_scope explicitly rejects absolute paths; use relative paths such as "
                f"['.'] or ['src']. Invalid entries: {absolute_entries!r}"
            )
    accept_unknown_git_state = injected.pop("accept_unknown_git_state", None)
    if accept_unknown_git_state is not None and type(accept_unknown_git_state) is not bool:
        raise TypeError("accept_unknown_git_state must be a bool or None")
    baseline_qualification = injected.pop("baseline_qualification", None)
    if baseline_qualification is not None and not isinstance(baseline_qualification, dict):
        raise TypeError("baseline_qualification must be a JSON object or None")
    memory_limit = injected.pop("memory_limit", 5)
    if type(memory_limit) is not int:
        raise TypeError("memory_limit must be an int between 1 and 20")
    if not 1 <= memory_limit <= 20:
        raise ValueError("memory_limit must be between 1 and 20")
    adoption_approval_id = injected.pop("adoption_approval_id", None)
    if adoption_approval_id is not None and (
        not isinstance(adoption_approval_id, str) or not adoption_approval_id.strip()
    ):
        raise TypeError("adoption_approval_id must be a non-empty string or None")
    trust_domain = injected.pop("trust_domain", None) or os.environ.get("ANCHOR_TRUST_DOMAIN")
    if trust_domain is not None and (
        not isinstance(trust_domain, str) or not trust_domain.strip()
    ):
        raise TypeError("trust_domain must be a non-empty string or None")

    from odibi_anchor._dispatcher._enforcement import classify_problem_rigor
    problem_rigor = classify_problem_rigor(
        " ".join(filter(None, [task_text, str(injected.get("goal") or "")])),
        mode=str(injected.get("mode", "planning")),
        priority=injected.get("priority"),
    )

    # ── Problem Record handling ─────────────────────────────────────────────
    problem_arg = injected.pop("problem", None)
    create_problem = bool(injected.pop("create_problem", False))
    # A replacement task never inherits links from the previous task.
    active_problem = problem_arg
    artifact_root = Path(problems_dir).parent if problems_dir is not None else None

    if active_problem and artifact_root is not None:
        from odibi_anchor._dispatcher._problem import problem_action

        projection = problem_action(
            artifact_root,
            "resume",
            active_problem,
            output_format="dict",
        )
        problem_facts = list(injected.get("known_facts") or [])
        problem_facts.extend([
            f"Active Problem Record: {active_problem} (stage {projection['stage']}/7, status={projection['status']})",
            f"Problem next action: {projection.get('next_action') or 'review the record'}",
        ])
        injected["known_facts"] = problem_facts

    work_item_arg = injected.pop("work_item", None)
    linked_work_item = None
    if work_item_arg is not None:
        linked_work_item = str(work_item_arg).strip().upper()
        if re.fullmatch(r"WI-\d{4}-\d{4}", linked_work_item) is None:
            raise ValueError("work_item must match WI-YYYY-NNNN")
        if artifact_root is None or not (artifact_root / "work_items" / f"{linked_work_item}.md").is_file():
            raise FileNotFoundError(f"Work Item does not exist: {linked_work_item}")
        from odibi_anchor._dispatcher._work_item import work_item_action

        work_item_result = cast(
            dict[str, Any],
            work_item_action(artifact_root, "show", linked_work_item, output_format="dict"),
        )
        work_item_record = cast(dict[str, Any], work_item_result["record"])
        if work_item_record["status"] in {"completed", "cancelled"}:
            raise ValueError("work_item must be open for implementation authority")
        if active_problem and work_item_record["problem_record"] != active_problem:
            raise ValueError("work_item is linked to a different Problem Record")
        supplied_spec = injected.get("spec")
        if supplied_spec and work_item_record["specification"] != str(supplied_spec).strip():
            raise ValueError("work_item is linked to a different Specification")
        work_item_facts = list(injected.get("known_facts") or [])
        work_item_facts.append(f"Linked Work Item: {linked_work_item}")
        injected["known_facts"] = work_item_facts

    # ── Spec handling (Phase 2) ──────────────────────────────────────────────
    spec_arg = injected.pop("spec", None)
    pending_spec_name = getattr(session_state, "pending_task_spec_name", None)
    inherited_pending = (
        spec_arg is None and bool(pending_spec_name) and specs_dir is not None
    )

    if spec_arg is not None and specs_dir is not None:
        spec_data = _resolve_and_parse_spec(spec_arg, specs_dir)
    elif inherited_pending and specs_dir is not None:
        spec_data = _resolve_and_parse_spec(pending_spec_name, specs_dir)

    if (spec_arg is not None and specs_dir is not None) or inherited_pending:

        # Inject success_criteria → acceptance_criteria (append)
        if spec_data.get("success_criteria"):
            existing_criteria = list(injected.get("acceptance_criteria") or [])
            spec_criteria = [f"[spec] {c}" for c in spec_data["success_criteria"]]
            injected["acceptance_criteria"] = existing_criteria + spec_criteria

        # Inject spec metadata → known_facts
        auto_facts = list(injected.get("known_facts") or [])
        auto_facts.append(
            f"Linked spec: {spec_data['name']} (status={spec_data['status']}, "
            f"{len(spec_data.get('success_criteria', []))} criteria)"
        )
        injected["known_facts"] = auto_facts

    # ── Existing session state injection ─────────────────────────────────────
    auto_facts = list(injected.get("known_facts") or [])
    auto_facts.append(
        f"Problem Record rigor: level {problem_rigor['level']} ({problem_rigor['label']}) — "
        f"{problem_rigor['reason']}"
    )
    if files_changed:
        auto_facts.append(f"Files changed this session: {sorted(files_changed)}")
    if frame and frame.data_context.tables_profiled:
        auto_facts.append(f"Tables already profiled: {list(frame.data_context.tables_profiled.keys())}")
    if frame and frame.code_context.files_changed:
        auto_facts.append(f"Code files in frame: {list(frame.code_context.files_changed)}")
    if auto_facts:
        injected["known_facts"] = auto_facts

    # Evaluate the complete replacement candidate, never the previous task's
    # links, ledgers, kernel, or facts.
    from types import SimpleNamespace

    from odibi_anchor.planning._task_policy import BpsKernel, evaluate_fresh_task_policies
    has_linked_spec = (spec_arg is not None and specs_dir is not None) or inherited_pending
    spec_phases = (spec_data.get("phases") if has_linked_spec else None) or []
    phase_count = max(1, len(spec_phases or injected.get("action_plan") or injected.get("phases") or ()))
    current_phase = 1
    if spec_phases:
        complete = {"done", "complete", "completed"}
        current_phase = next((index for index, phase in enumerate(spec_phases, 1)
                              if str(phase.get("status", "") if isinstance(phase, dict) else "").lower()
                              not in complete), phase_count)
    staged_facts = tuple(str(item) for item in (injected.get("known_facts") or ()))
    staged_uncertainties = tuple(str(item) for item in (
        injected.get("open_questions") or injected.get("evidence_gaps") or ()
    ))
    pr_draft_request = injected.get("generate_pr_draft")
    if pr_draft_request is not None and type(pr_draft_request) is not bool:
        raise TypeError("generate_pr_draft must be a bool or None")
    candidate_kernel = BpsKernel(
        problem=str(task_text or injected.get("subject") or injected.get("goal") or ""),
        intended_outcome=str(injected.get("desired_outcome") or injected.get("goal") or ""),
        known_facts=staged_facts, material_uncertainties=staged_uncertainties,
        next_check=str((injected.get("recommended_discovery_steps") or [""])[0]),
    )
    candidate_state = SimpleNamespace(
        anchor_home=getattr(session_state, "anchor_home", None),
        project_root=getattr(session_state, "project_root", None),
        artifact_root=getattr(session_state, "artifact_root", None),
        target_root=getattr(session_state, "target_root", None),
        active_project=getattr(session_state, "active_project", None),
        session_id=getattr(session_state, "session_id", None),
        managed_artifact_ledger=(), evidence_ledger=(),
        guidance_attestations=tuple(getattr(session_state, "guidance_attestations", ())),
        observed_effects=(), intended_pr_paths=(), repository_snapshot=None,
        repository_pr_config=None,
        task_verification_epoch=0,
    )
    candidate_context, task_policies = evaluate_fresh_task_policies(
        profile, session_state=candidate_state, current_action="task",
        current_effect="artifact_write", explicit_problem_requested=bool(create_problem or problem_arg),
        explicit_spec_requested=spec_arg is not None, linked_problem=active_problem,
        linked_spec=(spec_data.get("name") if has_linked_spec else None),
        persisted_spec=has_linked_spec,
        phase_count=phase_count, current_phase=current_phase, bps_kernel=candidate_kernel,
        referenced_facts=staged_facts,
        source_files_changed=tuple(sorted(files_changed)),
    )
    auto_problem_required = task_policies.problem_record.disposition == "required" and profile.rigor != "direct"
    auto_problem_intent = (create_problem or auto_problem_required) and not active_problem
    if auto_problem_intent and artifact_root is None:
        raise ValueError("create_problem requires a managed artifact root")

    # Inject risks from session debt
    auto_risks = list(injected.get("risks") or [])
    if not has_linked_spec and task_policies.specification.disposition == "recommended":
        auto_risks.append(
            "No spec linked. Consider creating one with "
            'anchor("spec", "create", name="...") for traceability.'
        )
    if task_policies.problem_record.disposition == "recommended" and not active_problem:
        auto_risks.append(
            "A Problem Record is recommended for this task. Re-run with "
            "create_problem=True or problem='PRB-...'."
        )
    if learn_debt:
        auto_risks.append(
            "Prior session has unresolved learning assessment debt; use canonical "
            "anchor('learning', 'capture|assess', ...) recovery"
        )
    if task_policies.work_items.disposition in {"recommended", "required"}:
        obligation = "required" if task_policies.work_items.disposition == "required" else "recommended"
        if task_policies.work_items.depth == "set":
            auto_risks.append(
                f"A provider-neutral work-item set is {obligation}. Decompose only team-visible outcomes "
                "with anchor('work_item', 'create', ...), not every agent execution step."
            )
        else:
            auto_risks.append(
                f"One provider-neutral work-item draft is {obligation}. Create it with "
                "anchor('work_item', 'create', ...) before optional provider publication."
            )
    if auto_risks:
        injected["risks"] = auto_risks

    try:
        from odibi_anchor.assurance import build_assurance_plan
        assurance_plan = build_assurance_plan(profile)
        assurance_diagnostics: tuple[str, ...] = ()
    except Exception as exc:
        assurance_plan = None
        assurance_diagnostics = (
            f"task-staging:{type(exc).__name__}:{str(exc)[:240]}",
        )

    # Inject _already_called from frame actions (internal kwarg, not user-facing)
    if frame and frame.actions:
        injected["_already_called"] = [a.tool for a in frame.actions]

    # Bootstrap consumes this staging envelope before invoking the planner.  It
    # contains no live mutable session objects and is committed only after the
    # planner result passes readiness.
    injected["_task_policy_stage"] = {
        "profile": profile,
        "context": candidate_context,
        "bps_kernel": candidate_kernel,
        "referenced_facts": staged_facts,
        "linked_problem": active_problem,
        "linked_spec": spec_data.get("name") if has_linked_spec else None,
        "linked_work_item": linked_work_item,
        "persisted_spec_name": spec_data.get("name") if has_linked_spec else None,
        "active_spec": spec_data if has_linked_spec else None,
        "explicit_problem_requested": bool(create_problem or problem_arg),
        "explicit_spec_requested": spec_arg is not None,
        "explicit_pr_draft_requested": pr_draft_request,
        "repository_scope": tuple(repository_scope or ()),
        "accept_unknown_git_state": accept_unknown_git_state,
        "baseline_qualification": baseline_qualification,
        "memory_limit": memory_limit,
        "adoption_approval_id": adoption_approval_id.strip() if adoption_approval_id else None,
        "trust_domain": trust_domain.strip() if trust_domain else None,
        "continuation": continuation,
        "assurance_plan": assurance_plan,
        "assurance_diagnostics": assurance_diagnostics,
        "phase_count": phase_count,
        "current_phase": current_phase,
        "auto_problem": ({
            "title": str(injected.get("subject") or injected.get("goal") or "Task investigation"),
            "definition": str(injected.get("background") or injected.get("current_state") or ""),
            "decision_needed": str(injected.get("goal") or ""),
            "rigor_level": profile.rigor if profile.rigor != "direct" else "compact",
        } if auto_problem_intent else None),
    }
    # Planning and enforcement must use the exact same normalized profile. This
    # private value is consumed by task_execution_context and is never serialized.
    injected["_task_profile"] = profile
    injected["_specification_disposition"] = task_policies.specification.disposition
    injected["_assurance_plan"] = assurance_plan
    injected["_assurance_diagnostics"] = assurance_diagnostics

    return injected


def _resolve_and_parse_spec(spec_arg: str, specs_dir: str | Path) -> dict:
    """Resolve a spec argument to a parsed spec dict.

    Args:
        spec_arg: Spec filename (e.g., "FEATURE_X_SPEC.md") or name fragment.
        specs_dir: Path to the specs/ directory.

    Returns:
        Parsed spec dict from _spec_parser.parse_spec().

    Raises:
        FileNotFoundError: If spec file doesn't exist.
        ValueError: If spec status is not ready, in-progress, or executing.
    """
    from odibi_anchor.codebase._spec_parser import find_spec, parse_spec

    specs_path = Path(specs_dir)

    # Try direct path first
    direct = specs_path / spec_arg
    if direct.exists():
        parsed = parse_spec(direct)
    else:
        # Try as name fragment
        parsed = find_spec(specs_path, spec_arg.replace("_SPEC.md", "").replace("_SPEC", ""))
        if parsed is None:
            raise FileNotFoundError(
                f"Spec not found: '{spec_arg}' in {specs_path}. "
                f"Use anchor('spec') to list available specs."
            )

    # Validate status
    valid_statuses = {"ready", "in-progress", "executing"}
    if parsed["status"] not in valid_statuses:
        raise ValueError(
            f"Spec '{parsed['name']}' has status '{parsed['status']}' — "
            f"expected one of {valid_statuses}. "
            "Only specs with status 'ready', 'in-progress', or 'executing' can be linked to tasks."
        )

    return parsed
