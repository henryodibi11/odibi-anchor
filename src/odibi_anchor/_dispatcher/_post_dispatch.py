"""Post-dispatch logic — extracted from anchor() in agent_init.py.

Handles post-task readiness enforcement, mode validation, learn content
validation, learn debt clearing, and same-invocation planning advisories.
"""
from __future__ import annotations


def post_commit_structured_assessment(
    result, *, session_timings, session_files_changed, session_state,
) -> None:
    """Best-effort identifier-only bookkeeping after an assessment commits."""
    assessment = result.get("assessment", {}) if isinstance(result, dict) else {}
    assessment_id = assessment.get("assessment_id")
    if not assessment_id:
        return
    observation_ids = sorted(str(item) for item in assessment.get("observation_ids", []))
    root = str(
        getattr(session_state, "artifact_root", None)
        or getattr(session_state, "target_root", None)
        or "."
    )
    try:
        from odibi_anchor._dispatcher._compliance import _compliance_audit
        from odibi_anchor.codebase._memory_db import insert_audit
        audit = _compliance_audit()
        if session_files_changed:
            insert_audit(
                project=root,
                score=audit["score"], max_score=audit["max_score"], rating=audit["rating"],
                gaps=audit["gaps"], files_changed=sorted(session_files_changed), files_created=[],
                actions=[item["action"] for item in session_timings if item.get("error") is None],
                total_actions=audit["stats"]["total_actions"],
                total_time_ms=audit["stats"]["total_time_ms"],
                errors=audit["stats"]["errors_encountered"], learnings=observation_ids,
                framework_epoch="structured-assessment-v1",
            )
    except Exception as exc:
        from odibi_anchor._utils._session_state import record_degraded
        record_degraded("structured_assessment_audit", exc)
    try:
        if session_files_changed:
            from odibi_anchor._dispatcher._session_health import capture_session_health
            capture_session_health(
                root, session_files_changed, session_timings,
            )
    except Exception as exc:
        from odibi_anchor._utils._session_state import record_degraded
        record_degraded("structured_assessment_health", exc)
    try:
        notebook_path = getattr(session_state, "notebook_path", None)
        if notebook_path:
            from odibi_anchor._dispatcher._memory import append_assessment_to_notebook
            append_assessment_to_notebook(
                notebook_path, str(assessment_id), observation_ids,
            )
    except Exception as exc:
        from odibi_anchor._utils._session_state import record_degraded
        record_degraded("structured_assessment_notebook", exc)


def _persist_terminal_task_if_ready(
    result, *, action, args, session_timings, session_files_changed, session_state,
) -> None:
    """Persist the immutable terminal projection at successful lifecycle boundaries."""
    if not isinstance(result, dict):
        return
    selector = str(args[0]).lower().strip() if action == "learning" and args else ""
    status = None
    assessment = None
    unavailable: list[str] = []
    epoch = session_state.task_verification_epoch
    if selector == "assess":
        assessment = result.get("assessment")
        from odibi_anchor._dispatcher._operating_protocol import latest_delivery_gate_passed

        if latest_delivery_gate_passed(session_timings, epoch=epoch):
            status = "completed"
        elif not session_files_changed:
            profile = getattr(session_state, "active_task_profile", None)
            if profile is not None and profile.execution_mode == "read_only":
                status = "completed"
    elif selector == "safe_stop" and session_state.terminal_status in {"blocked", "failed"}:
        status = session_state.terminal_status
        samples = result.get("samples")
        if isinstance(samples, dict) and isinstance(samples.get("unavailable_evidence"), list):
            unavailable = samples["unavailable_evidence"]
    if status is None:
        return
    from odibi_anchor._dispatcher._boot import _ENV
    from odibi_anchor.codebase._task_execution import (
        build_terminal_projection,
        inspect_terminal_records,
        persist_terminal_record,
    )
    existing = inspect_terminal_records(
        _ENV["memory_db"], task_window_id=session_state.task_window_id,
    )["records"]
    if existing:
        retained = existing[0]
        result["terminal_task_record"] = {
            "record_id": retained["record_id"],
            "task_window_id": retained["task_window_id"],
            "record_sha256": retained["record_sha256"],
            "record": retained["record"],
        }
        from odibi_anchor.codebase._task_authority import close_accepted_task

        result["accepted_task_closure"] = close_accepted_task(
            _ENV["memory_db"], task_window_id=session_state.task_window_id,
            terminal_status=status,
        )
        _snapshot_durable_state(result, memory_db=_ENV["memory_db"])
        return
    record = build_terminal_projection(
        session_state=session_state, session_timings=session_timings,
        files_changed=session_files_changed, terminal_status=status,
        assessment=assessment, unavailable=unavailable, memory_db=_ENV["memory_db"],
    )
    result["terminal_task_record"] = persist_terminal_record(_ENV["memory_db"], record)
    from odibi_anchor.codebase._task_authority import close_accepted_task

    result["accepted_task_closure"] = close_accepted_task(
        _ENV["memory_db"], task_window_id=session_state.task_window_id,
        terminal_status=status,
    )
    _snapshot_durable_state(result, memory_db=_ENV["memory_db"])


def _snapshot_durable_state(result, *, memory_db: str) -> None:
    """Checkpoint configured durable state after successful authority writes."""
    from odibi_anchor._dispatcher._boot import _ENV

    durable_root = _ENV.get("durable_root")
    authority_id = _ENV.get("authority_id")
    if not durable_root:
        return
    if not authority_id:
        raise RuntimeError(
            "BLOCKED: ANCHOR_AUTHORITY_ID is required when ANCHOR_DURABLE_ROOT is configured"
        )
    if _ENV.get("trust_domain") != "work":
        raise RuntimeError(
            "BLOCKED: ANCHOR_TRUST_DOMAIN=work is required when ANCHOR_DURABLE_ROOT is configured"
        )
    from odibi_anchor.durability import snapshot_state

    try:
        result["durable_state"] = snapshot_state(
            source_db=memory_db,
            source_artifacts=(
                _ENV["runtime_paths"].anchor_home / "workspace" / "projects"
            ),
            durable_root=durable_root,
            authority_id=authority_id,
            databricks=bool(_ENV.get("is_databricks")),
            retention_days=_ENV.get("retention_days"),
            minimum_snapshots=_ENV.get("retention_minimum_snapshots"),
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise RuntimeError(
            f"BLOCKED: active state could not be durably snapshotted: {type(exc).__name__}: {exc}"
        ) from exc


def _attach_learning_candidate_suggestions(result, *, session_timings, session_state) -> None:
    """Prefill evidence-backed capture candidates without asserting their truth."""
    if not isinstance(result, dict):
        return
    epoch = session_state.task_verification_epoch
    window = session_timings[epoch:] if isinstance(epoch, int) else []
    failures = [row for row in window if row.get("error")]
    candidates = []
    for index, row in enumerate(failures[:5], 1):
        action = str(row.get("action") or "unknown")
        project = session_state.active_project
        candidates.append({
            "observation_type": "friction",
            "summary": f"The {action} action failed before this task reached its terminal gate.",
            "signal_key": f"task-action-failure.{action}",
            "impact": "medium",
            "applicability_scope": "project_local" if project else "workbench",
            "project_refs": [project] if project else [],
            "work_package_refs": [value for value in (
                session_state.linked_problem, session_state.linked_spec, session_state.linked_work_item,
            ) if value],
            "environment_refs": [],
            "provenance": {
                "source_action": action, "source_version": f"candidate-{index}",
            },
            "evidence": [{
                "reference_type": "session",
                "reference": f"session:{session_state.session_id}:task:{session_state.task_window_id}:action:{action}",
                "summary": "Anchor retained a failed dispatch timing for this task action.",
            }],
        })
    result["learning_candidate_suggestions"] = {
        "candidates": candidates,
        "auto_captured": False,
        "authority": "proposal_only",
        "instruction": (
            "Review each candidate; call anchor('learning', 'capture', ...) only when its bounded "
            "claim is reusable and supported, then assess the returned Observation IDs."
        ),
    }


def _verify_selected_memory_candidates(result, *, session_state) -> None:
    """Run selected checks plus a fair bounded project-local verifier sweep."""
    if not isinstance(result, dict):
        return
    selection_ids = list(dict.fromkeys(getattr(session_state, "memory_selections", ()) or ()))[:5]
    import sqlite3

    from odibi_anchor._dispatcher._boot import _ENV
    from odibi_anchor.codebase._memory_verifier import run_installed_verifier

    connection = sqlite3.connect(_ENV["memory_db"])
    try:
        selected_rows = []
        if selection_ids:
            placeholders = ",".join("?" for _ in selection_ids)
            selected_rows = connection.execute(
                "SELECT s.memory_id FROM memory_selections s JOIN memories m ON m.id=s.memory_id "
                f"WHERE s.selection_id IN ({placeholders}) AND s.task_window_id=? "
                "AND m.status IN ('candidate','active') ORDER BY s.selected_at,s.selection_id",
                [*selection_ids, session_state.task_window_id],
            ).fetchall()
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        run_order = (
            "(SELECT count(*) FROM memory_verifier_runs v WHERE v.memory_id=m.id),"
            if "memory_verifier_runs" in tables else ""
        )
        sweep_rows = connection.execute(
            "SELECT m.id FROM memories m WHERE m.project=? "
            "AND m.status IN ('candidate','active') AND m.source LIKE 'structured_learning:%' "
            f"ORDER BY {run_order}m.created,m.id LIMIT 5",
            (session_state.active_project or "__unresolved_project__",),
        ).fetchall()
    finally:
        connection.close()
    selected_ids = list(dict.fromkeys(row[0] for row in selected_rows))[:5]
    sweep_ids = [row[0] for row in sweep_rows if row[0] not in selected_ids][:5]
    memory_ids = [*selected_ids, *sweep_ids]
    if not memory_ids:
        return
    baseline = getattr(session_state, "task_repository_baseline", None)
    if baseline is None or not session_state.target_root:
        result["memory_candidate_verification"] = {
            "bounded_limit": 10,
            "results": [
                {"memory_id": memory_id, "status": "unavailable",
                 "reason": "repository task authority unavailable"}
                for memory_id in memory_ids
            ],
            "selected_count": len(selected_ids),
            "sweep_count": len(sweep_ids),
            "authority_mutation": "none",
        }
        return
    from odibi_anchor._repository_snapshot import is_databricks_git_folder_baseline

    if is_databricks_git_folder_baseline(baseline):
        result["memory_candidate_verification"] = {
            "bounded_limit": 10,
            "results": [
                {
                    "memory_id": memory_id,
                    "status": "unavailable",
                    "reason": (
                        "memory verification requires canonical local Git history; "
                        "Databricks Git Folder task evidence does not provide it"
                    ),
                }
                for memory_id in memory_ids
            ],
            "selected_count": len(selected_ids),
            "sweep_count": len(sweep_ids),
            "authority_mutation": "none",
        }
        return
    verified = []
    for memory_id in memory_ids:
        try:
            run = run_installed_verifier(
                _ENV["memory_db"], memory_id=memory_id,
                project_id=session_state.active_project,
                task_window_id=session_state.task_window_id,
                target_root=session_state.target_root,
                configured_target_ref=baseline.configured_target_ref,
            )["run"]
        except ValueError as exc:
            verified.append({
                "memory_id": memory_id, "status": "ineligible", "reason": str(exc),
            })
            continue
        verified.append({
            "memory_id": memory_id, "status": run["result_state"],
            "run_id": run["run_id"], "contradiction_status": run["contradiction_status"],
        })
    result["memory_candidate_verification"] = {
        "bounded_limit": 10,
        "selected_limit": 5,
        "sweep_limit": 5,
        "selected_count": len(selected_ids),
        "sweep_count": len(sweep_ids),
        "results": verified,
        "authority_mutation": "none_until_terminal_finalization",
    }


def run_post_dispatch(
    action, result, err, args, kwargs, *,
    session_timings, session_files_changed, session_state,
    planning_required_actions, action_contract=None, task_stage=None,
    invocation_resolution=None, pre_task_decision=None,
):
    """Run all post-dispatch enforcement checks.

    Returns (possibly modified) result. Raises RuntimeError for blocking violations.
    """
    from odibi_anchor._dispatcher._effects import dispatch_succeeded
    _dispatch_ok = dispatch_succeeded(action, result, err)
    if action == "gate" and _dispatch_ok:
        _verify_selected_memory_candidates(result, session_state=session_state)
    if isinstance(result, dict) and session_timings and session_timings[-1].get("action") == action:
        # Timings prove invocation. Retain a compact result fact so assurance can
        # distinguish a completed result from invocation alone without copying output.
        result_kind = result.get("kind")
        if isinstance(result_kind, str) and result_kind:
            session_timings[-1]["result"] = result_kind
        if action == "test":
            metrics = result.get("metrics")
            exit_code = metrics.get("exit_code") if isinstance(metrics, dict) else None
            if isinstance(exit_code, int) and not isinstance(exit_code, bool):
                session_timings[-1]["exit_code"] = exit_code
    # ── Post-task readiness enforcement: block if readiness too low ──
    _MIN_READINESS = 40
    if action == "task" and isinstance(result, dict):
        _readiness = result.get("readiness", {})
        _readiness_score = _readiness.get("score", 100) if isinstance(_readiness, dict) else 100
        if _readiness_score < _MIN_READINESS:
            _missing = _readiness.get("missing_details", []) if isinstance(_readiness, dict) else []
            _missing_str = "\n  - ".join(_missing[:5]) if _missing else "unknown gaps"
            if session_timings and session_timings[-1]["action"] == "task":
                session_timings[-1]["passed"] = False
            raise RuntimeError(
                f"BLOCKED: Task readiness too low: {_readiness_score}% (minimum: {_MIN_READINESS}%).\n"
                f"Missing:\n  - {_missing_str}\n"
                f"Add more kwargs (goal=, known_facts=, constraints=, acceptance_criteria=) "
                f"and re-run anchor(\"task\") until readiness ≥ {_MIN_READINESS}%."
            )

    # ── Post-task mode validation: record mode for later mismatch detection ──
    if action == "task" and _dispatch_ok:
        from odibi_anchor.planning._task_profile import TaskProfile, normalize_task_profile

        _task_mode = kwargs.get("mode", "planning")
        if isinstance(result, dict) and task_stage and task_stage.get("context") is not None:
            from odibi_anchor._dispatcher._guidance import select_guidance
            from odibi_anchor.planning._task_policy import evaluate_task_policies
            guidance_context = task_stage["context"]
            work_item_policy = evaluate_task_policies(
                task_stage["profile"], guidance_context,
            ).work_items
            result["work_item_policy"] = {
                "disposition": work_item_policy.disposition,
                "depth": work_item_policy.depth,
                "rule_ids": list(work_item_policy.rule_ids),
                "reasons": list(work_item_policy.reasons),
            }
            guidance_targets = select_guidance(guidance_context)
            result["canonical_guidance"] = [
                {"skill": target.skill}
                for target in guidance_targets
            ]
            from pathlib import Path

            from odibi_anchor._dispatcher._boot import _RUNTIME_PATHS
            from odibi_anchor._dispatcher._references import task_reference_guidance
            from odibi_anchor.assurance import overlay_input_from_profile, select_overlays
            target_root = getattr(session_state, "target_root", None)
            domains = kwargs.get("domains") or ()
            traits = kwargs.get("traits") or ()
            reference_query = " ".join(str(value) for value in (
                args[0] if args else "", kwargs.get("goal", ""),
                kwargs.get("desired_outcome", ""),
                domains if isinstance(domains, str) else " ".join(domains),
                traits if isinstance(traits, str) else " ".join(traits),
            ) if value)
            try:
                overlay_selection = None
                assurance_plan = task_stage.get("assurance_plan") if task_stage else None
                if assurance_plan is not None:
                    overlay_selection = select_overlays(overlay_input_from_profile(
                        task_stage["profile"], assurance_plan,
                    ))
                result["reference_guidance"] = task_reference_guidance(
                    _RUNTIME_PATHS.resource_root / ".assistant" / "references",
                    reference_query,
                    task_profile=task_stage["profile"],
                    changed_files=sorted(session_files_changed),
                    repository_signals=[
                        signal
                        for signal, names in (
                            ("python", ("pyproject.toml", "setup.py", "setup.cfg", "tox.ini")),
                            ("databricks", ("databricks.yml", "databricks.yaml")),
                        )
                        if target_root and any(
                            (Path(target_root) / name).is_file()
                            for name in names
                        )
                    ],
                    overlay_selection=overlay_selection,
                )
            except (OSError, RuntimeError, ValueError) as exc:
                result["reference_guidance"] = []
                result.setdefault("risks", []).append(
                    f"Engineering reference guidance is unavailable: {exc}"
                )
            from odibi_anchor._dispatcher._capture_standards import (
                capture_standards_contract,
                project_capture_guidance,
            )
            result["capture_guidance"] = project_capture_guidance(
                capture_standards_contract(),
                task_text=reference_query,
                profile=task_stage["profile"],
                artifacts=getattr(session_state, "managed_artifact_ledger", ()),
                expected_output_format=kwargs.get("expected_output_format"),
            )
        if session_timings:
            session_timings[-1]["task_mode"] = _task_mode
        # Validate and capture source scope after acceptance readiness, before any
        # automatic managed Problem Record write.
        task_baseline = None
        staged_profile = task_stage.get("profile") if task_stage else None
        if staged_profile is not None and staged_profile.execution_mode == "source_change":
            if not session_state.target_root or not session_state.artifact_root:
                raise RuntimeError("source-change task requires a resolvable target and artifact root")
            repository_provider = getattr(session_state, "repository_provider", None)
            from odibi_anchor._repository_snapshot import canonical_local_git_available
            local_git_available = canonical_local_git_available(session_state.target_root)
            if repository_provider is not None and not local_git_available:
                from odibi_anchor._repository_snapshot import (
                    capture_databricks_git_folder_task_baseline,
                    databricks_task_baseline_projection,
                )
                task_baseline = capture_databricks_git_folder_task_baseline(
                    session_state.target_root,
                    repository_provider,
                    task_stage.get("repository_scope") if task_stage else None,
                    accept_unknown_git_state=(
                        task_stage.get("accept_unknown_git_state") is True if task_stage else False
                    ),
                )
                if isinstance(result, dict):
                    result["repository_evidence"] = databricks_task_baseline_projection(
                        task_baseline
                    )
                    scope_example = list(task_baseline.repository_scope)
                    result["databricks_implementation_guidance"] = {
                        "invocation": (
                            'anchor("task", "<description>", goal="<goal>", mode="implementation", '
                            'work_type="change", execution_mode="source_change", '
                            f"repository_scope={scope_example!r}, accept_unknown_git_state=True, "
                            'acceptance_criteria=["<completion check>"])'
                        ),
                        "stale_agent_recovery": (
                            "Refresh and rerun the current Odibi Anchor bootstrap, then use the "
                            "implementation invocation above; do not fall back to documentation mode."
                        ),
                    }
                    result.setdefault("risks", []).append(
                        "Databricks Repos cannot prove pre-existing Git dirtiness. Source authority "
                        "is limited to exact task-start bytes in repository_scope."
                    )
            else:
                adoption_approval_id = task_stage.get("adoption_approval_id") if task_stage else None
                if adoption_approval_id:
                    from odibi_anchor._dispatcher._boot import _ENV
                    from odibi_anchor.codebase._adopted_dirty import (
                        AdoptionUnavailable,
                        prepare_adoption,
                        record_adoption_refusal,
                    )

                    try:
                        prepared = prepare_adoption(
                            _ENV["memory_db"], approval_id=adoption_approval_id,
                            project_id=session_state.active_project,
                            target_root=session_state.target_root,
                            artifact_root=session_state.artifact_root,
                            trust_domain=task_stage.get("trust_domain") or "",
                        )
                    except AdoptionUnavailable as exc:
                        record_adoption_refusal(
                            _ENV["memory_db"], exc, operation="prepare",
                            approval_id=adoption_approval_id,
                        )
                        raise
                    task_baseline = prepared["baseline"]
                    task_stage["prepared_adoption"] = prepared
                    if isinstance(result, dict):
                        result["repository_evidence"] = {
                            "authority_kind": "adopted",
                            "approval_id": prepared["approval_id"],
                            "challenge_sha256": prepared["challenge_sha256"],
                            "prior_task_window_id": prepared["prior_task_window_id"],
                            "changed_paths": prepared["subject"]["repository"]["changed_paths"],
                        }
                else:
                    from odibi_anchor._pr_readiness import load_pr_config
                    from odibi_anchor._repository_snapshot import capture_task_repository_baseline
                    config = load_pr_config(session_state.artifact_root)
                    task_baseline = capture_task_repository_baseline(
                        session_state.target_root, config["default_target_ref"],
                    )
        # Validate baseline qualification before any managed-record write. A
        # rejected task must leave no durable artifact or task-visible state.
        task_window_id = getattr(session_state, "task_window_id", None)
        task_qualification = None
        if task_window_id is not None:
            from odibi_anchor._dispatcher._baseline_qualification import qualify_task_baseline

            task_qualification = qualify_task_baseline(
                task_baseline,
                task_window_id=task_window_id,
                execution_mode=(
                    staged_profile.execution_mode if staged_profile is not None else "analysis"
                ),
                target_root=session_state.target_root,
                request=(task_stage.get("baseline_qualification") if task_stage else None),
            )
        # Materialize any required managed record before replacing old state.
        # Failure here leaves the complete previous task untouched.
        created = None
        if task_stage and task_stage.get("auto_problem"):
            from odibi_anchor._dispatcher._problem import problem_action
            created = problem_action(
                session_state.artifact_root, "create", project_id=session_state.active_project,
                output_format="dict", **task_stage["auto_problem"],
            )
        assurance_plan = task_stage.get("assurance_plan") if task_stage else None
        if isinstance(result, dict) and assurance_plan is not None:
            try:
                from dataclasses import replace

                from odibi_anchor.assurance import (
                    evaluate_assurance,
                    normalize_assurance_evidence,
                )

                assert task_stage is not None
                assurance_context = replace(
                    task_stage["context"],
                    current_action="task",
                    current_evidence_entries=(),
                    guidance_attestations=(),
                    task_verification_epoch=len(session_timings),
                )
                assessment = evaluate_assurance(
                    assurance_plan,
                    normalize_assurance_evidence(assurance_context, session_timings),
                )
                result["assurance"] = {
                    "plan": assurance_plan.to_dict(),
                    "assessment": assessment.to_dict(),
                }
            except Exception as exc:
                diagnostic = f"task-assessment:{type(exc).__name__}:{str(exc)[:240]}"
                result["assurance"] = {
                    "plan": assurance_plan.to_dict(),
                    "assessment": {
                        "mode": "shadow",
                        "status": "degraded",
                        "diagnostics": [diagnostic],
                    },
                }
                from odibi_anchor._utils._session_state import record_degraded
                record_degraded("assurance_shadow_task", diagnostic)
        from odibi_anchor._utils._session_state import reset_task_policy_state
        reset_task_policy_state(session_state)
        session_state.task_repository_baseline = task_baseline
        session_state.task_repository_baseline_qualification = task_qualification
        if task_qualification is not None and isinstance(result, dict):
            from dataclasses import asdict
            result["baseline_qualification"] = asdict(task_qualification)
        if isinstance(result, dict):
            session_state.task_handoff_context = {
                key: result[key]
                for key in (
                    "intent", "background", "scope", "constraints", "verification",
                    "guardrails", "risks", "resources",
                )
                if key in result
            }
        session_state.task_verification_epoch = len(session_timings)
        session_state.active_task_mode = _task_mode
        if isinstance(result, dict) and isinstance(result.get("task_profile"), dict):
            session_state.active_task_profile = TaskProfile.from_dict(result["task_profile"])
        else:
            session_state.active_task_profile = normalize_task_profile(
                legacy_mode=_task_mode,
                work_type=kwargs.get("work_type"),
                execution_mode=kwargs.get("execution_mode"),
                risk=kwargs.get("risk"),
                rigor=kwargs.get("rigor"),
                domains=kwargs.get("domains"),
                traits=kwargs.get("traits"),
                caller_required_evidence=kwargs.get("caller_required_evidence"),
                task_text=" ".join(str(part) for part in (
                    args[0] if args else "", kwargs.get("goal", ""),
                    kwargs.get("desired_outcome", ""),
                ) if part),
                priority=kwargs.get("priority"),
            )
        session_state.task_goal = str(
            kwargs.get("goal") or (args[0] if args else "")
        ).strip() or None
        session_state.task_tags = list(kwargs.get("tags") or [])
        if task_stage:
            session_state.bps_kernel = task_stage["bps_kernel"]
            session_state.referenced_facts = task_stage["referenced_facts"]
            session_state.active_task_profile = task_stage["profile"]
            session_state.active_assurance_plan = task_stage.get("assurance_plan")
            session_state.linked_problem = task_stage.get("linked_problem")
            _linked = task_stage.get("linked_spec")
            if _linked:
                from odibi_anchor._dispatcher._spec import canonical_spec_name
                _linked = canonical_spec_name(_linked)
            session_state.linked_spec = _linked
            session_state.linked_work_item = task_stage.get("linked_work_item")
            _persisted = task_stage.get("persisted_spec_name")
            session_state.persisted_spec_name = (
                canonical_spec_name(_persisted) if _persisted else None
            )
            session_state.spec_persisted = bool(
                session_state.linked_spec
                and session_state.persisted_spec_name == session_state.linked_spec
            )
            # Every accepted task starts with no transferable review authority.
            session_state.reviewed_spec_name = None
            session_state.spec_review_rating = None
            session_state.active_problem = session_state.linked_problem
            session_state.active_spec = task_stage.get("active_spec")
            session_state.explicit_problem_requested = task_stage.get("explicit_problem_requested", False)
            session_state.explicit_spec_requested = task_stage.get("explicit_spec_requested", False)
            session_state.explicit_pr_draft_requested = task_stage.get("explicit_pr_draft_requested")
            session_state.phase_count = task_stage.get("phase_count", 1)
            session_state.current_phase = task_stage.get("current_phase", 1)
        if created:
            from odibi_anchor._utils._session_state import record_managed_artifact
            session_state.linked_problem = created["problem_id"]
            session_state.active_problem = created["problem_id"]
            record_managed_artifact(session_state, created["artifact_path"], "problem",
                                    source="anchor:task:auto-problem",
                                    provenance={"problem_id": created["problem_id"]})

    checkpoint_marker = getattr(session_state, "checkpoint_in_progress", None)
    provisional_checkpoint_action = (
        checkpoint_marker is not None
        and action in {"preflight", "test", "gate", "learn", "learning"}
        and not (
            action in {"learn", "learning"}
            and checkpoint_marker.get("learn_phase") == "commit"
        )
    )
    if provisional_checkpoint_action:
        # Nested validation may shape its returned evidence but cannot pay debt,
        # publish effects, or mutate policy state before the checkpoint commits.
        return result

    if (action == "learning" and _dispatch_ok
            and (str(args[0]).lower().strip() if args else "list") == "assess"):
        post_commit_structured_assessment(
            result, session_timings=session_timings,
            session_files_changed=session_files_changed, session_state=session_state,
        )

    if action == "learning" and _dispatch_ok:
        _persist_terminal_task_if_ready(
            result, action=action, args=args, session_timings=session_timings,
            session_files_changed=session_files_changed, session_state=session_state,
        )

    if action == "gate" and _dispatch_ok:
        _attach_learning_candidate_suggestions(
            result, session_timings=session_timings, session_state=session_state,
        )

    # Ledger only resolved, successful writes. Read responses and error-shaped
    # dictionaries must never become managed artifacts.
    _write_success = (_dispatch_ok and isinstance(result, dict)
                      and result.get("write_performed") is True)
    _artifact_path = (result.get("path") or result.get("artifact_path")) if isinstance(result, dict) else None
    _managed_writes = result.get("managed_writes") if isinstance(result, dict) else None
    if action in {"problem", "spec", "work_item", "snapshot", "incident_snapshot", "observe_table"} and _write_success and (_artifact_path or _managed_writes):
        from odibi_anchor._utils._session_state import record_managed_artifact
        writes = _managed_writes or [
            {"path": str(_artifact_path), "kind": action}
        ]
        for write in writes:
            path = (write.get("path") or write.get("relative_path")) if isinstance(write, dict) else write
            kind = write.get("kind", action) if isinstance(write, dict) else action
            if path:
                record_managed_artifact(session_state, str(path), str(kind), source=f"anchor:{action}")

    if action == "work_item" and _dispatch_ok and isinstance(result, dict):
        attestations = result.get("accepted_attestations") or []
        if attestations:
            from odibi_anchor.planning._task_policy import EvidenceEntry
            for item in attestations:
                entry = EvidenceEntry(
                    item["id"], item["kind"], item["status"], item["source"],
                    item["observed_at"], item.get("provenance", {}),
                )
                session_state.guidance_attestations = [
                    current for current in session_state.guidance_attestations
                    if current.id != entry.id
                ] + [entry]

    # ── Track the active spec name so learnings can be linked to it (S-4) ──
    if action == "spec" and _dispatch_ok and isinstance(result, dict):
        _sname = result.get("spec_name") or result.get("name")
        if not _sname and result.get("path"):
            import os as _os
            _base = _os.path.splitext(_os.path.basename(result["path"]))[0]
            _sname = _base[:-5] if _base.endswith("_SPEC") else _base
        if _sname:
            from odibi_anchor._dispatcher._spec import canonical_spec_name
            _sname = canonical_spec_name(_sname)
            session_state.active_spec_name = _sname
        _spec_command = str(args[0]).lower().strip() if args else ""
        if (_sname and result.get("write_performed")
                and (_spec_command in {"create", "persist", "execute"}
                     or result.get("linked") or result.get("executing"))):
            if getattr(session_state, "linked_spec", None) != _sname:
                session_state.reviewed_spec_name = None
                session_state.spec_review_rating = None
            session_state.linked_spec = _sname
        if ((_spec_command == "persist" and result.get("created"))
                or (_spec_command == "execute" and result.get("executing"))) and _sname:
            session_state.persisted_spec_name = _sname
        session_state.spec_persisted = bool(
            getattr(session_state, "linked_spec", None)
            and getattr(session_state, "persisted_spec_name", None) == session_state.linked_spec
        )
        if result.get("executing"):
            # The next accepted task receives this exact persisted identity.
            # Readiness rejection never reaches this branch for a task and thus
            # cannot consume it; accepted task reset above does consume it.
            session_state.pending_task_spec_name = _sname
            if isinstance(result.get("active_spec"), dict):
                session_state.active_spec = result["active_spec"]
            session_state.phase_count = max(1, int(result.get("total_phases", 1)))
            phases = result.get("phases") or []
            complete = {"done", "complete", "completed"}
            session_state.current_phase = next(
                (index for index, phase in enumerate(phases, 1)
                 if str(phase.get("status", "")).lower() not in complete),
                session_state.phase_count,
            )
        elif result.get("write_performed") and result.get("updated") and result.get("spec_name") == session_state.linked_spec:
            session_state.current_phase = session_state.phase_count

    # ── Post-spec-review: record review rating in session state ──
    if action == "spec" and _dispatch_ok and isinstance(result, dict):
        _review_rating = result.get("rating")
        if _review_rating is not None:
            session_state.spec_review_rating = _review_rating
            _reviewed_name = result.get("spec_name") or result.get("name")
            if _reviewed_name:
                from odibi_anchor._dispatcher._spec import canonical_spec_name
                session_state.reviewed_spec_name = canonical_spec_name(_reviewed_name)

    # Problem actions establish one canonical investigation for later projections.
    if (action == "problem" and _dispatch_ok and isinstance(result, dict)
            and result.get("write_performed") is not False):
        _problem_id = result.get("problem_id")
        if result.get("closed"):
            session_state.active_problem = None
        elif _problem_id:
            session_state.active_problem = _problem_id
            session_state.linked_problem = _problem_id
        if result.get("accepted_evidence"):
            from odibi_anchor._utils._session_state import record_evidence
            for item in result["accepted_evidence"]:
                record_evidence(session_state, **item)

    # Evaluate each already-resolved observed effect after successful writes have
    # updated links, phase, artifact and evidence ledgers. The contract fallback
    # exists only for direct compatibility callers; production passes the result.
    resolution = invocation_resolution
    if resolution is None and action_contract is not None:
        from odibi_anchor._dispatcher._effects import resolve_invocation
        resolution = resolve_invocation(action_contract, args, kwargs)
    task_baseline = getattr(session_state, "task_repository_baseline", None)
    if (
        _dispatch_ok
        and resolution is not None
        and resolution.error is None
        and getattr(task_baseline, "evidence_kind", None) == "databricks_git_folder"
        and (action == "touched" or "source_write" in resolution.effects)
    ):
        write_path = (
            (args[0] if args else None)
            if action == "touched"
            else kwargs.get("target") or (args[0] if args else None)
        )
        if write_path:
            from odibi_anchor._repository_snapshot import acknowledge_databricks_task_writes
            session_state.task_repository_write_fingerprints = acknowledge_databricks_task_writes(
                task_baseline,
                session_state.task_repository_write_fingerprints,
                (str(write_path),),
                incremental=(action == "touched"),
            )
    if _dispatch_ok and resolution is not None:
        from odibi_anchor.planning._task_policy import evaluate_fresh_task_policies
        if resolution.error is None:
            effects = resolution.effects
            if isinstance(result, dict) and result.get("write_performed") is False:
                effects = tuple(effect for effect in effects if effect != "artifact_write")
            session_state.observed_effects.extend(effects)
            if session_state.active_task_profile is not None:
                for effect in effects:
                    evaluate_fresh_task_policies(
                        session_state.active_task_profile, session_state=session_state,
                        current_action=action, current_effect=effect,
                        source_files_changed=tuple(sorted(session_files_changed)),
                        checkpoint_final=bool(kwargs.get("final", False)),
                    )
            if (
                isinstance(result, dict)
                and action != "task"
                and "durable_state" not in result
                and {"governance_write", "artifact_write"}.intersection(effects)
            ):
                from odibi_anchor._dispatcher._boot import _ENV

                _snapshot_durable_state(result, memory_db=_ENV["memory_db"])

    # Tool findings are candidates, not automatically persisted evidence. This
    # makes the offer explicit while preserving human/agent judgment about materiality.
    if (
        _dispatch_ok
        and isinstance(result, dict)
        and getattr(session_state, "active_problem", None)
        and action in {
            "map", "impact", "consistency", "profile_table", "microscope",
            "case_file", "quality", "validate", "diff", "schema_diff",
            "known_error", "trace", "lookup", "review",
        }
    ):
        result["problem_evidence_candidate"] = {
            "problem_id": session_state.active_problem,
            "source": f"anchor:{action}",
            "observation": str(result.get("summary") or result.get("findings") or "")[:500],
            "persisted": False,
        }

    # ── Clear prior session learn debt after successful learn ──
    if action == "learn" and _dispatch_ok and session_state.prior_learn_debt:
        session_state.prior_learn_debt = False

    # ── Planning advisory on the successful task-required invocation itself ──
    attempt = getattr(pre_task_decision, "attempt", None)
    if (
        _dispatch_ok
        and getattr(pre_task_decision, "pre_task_access", None) == "task_required"
        and isinstance(attempt, int)
        and 3 <= attempt <= 7
    ):
        _nag = (
            "\n⚠ Planning not detected — run anchor(\"task\", goal=\"...\", mode=\"...\") now. "
            "Every session requires planning, including research/exploration."
        )
        if isinstance(result, str):
            result = result + _nag
        elif isinstance(result, dict):
            result.setdefault("risks", []).append(_nag.strip())
    # ── Shift-left advisory: post-hoc diagnostic without a prior pre-check (D-004) ──
    _POSTHOC = {"diff", "debug", "diagnose_empty", "explain_row", "trace_row"}
    if action in _POSTHOC and isinstance(result, dict) and _dispatch_ok:
        _pre_ran = any(
            t["action"] in ("pre_join", "pre_merge")
            and (t.get("passed") is True or ("passed" not in t and t.get("error") is None))
            for t in session_timings
        )
        if not _pre_ran:
            result.setdefault("suggested_next_actions", []).append(
                "NOTE (1-10-100): no anchor('pre_join'/'pre_merge') ran this session. "
                "Pre-checking keys before a join/merge catches this class of issue "
                "in 1 turn instead of debugging it after the fact."
            )

    return result
