"""Checkpoint action — atomic preflight, test, gate, and learning assessment."""
from __future__ import annotations

import copy
import hashlib
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def checkpoint(anchor_fn, session_files_changed, session_state, *args, **kwargs) -> dict | str:
    """Run a checkpoint with lifecycle state visible to nested calls."""
    session_timings = kwargs.pop("_session_timings", None)
    session_frame = kwargs.pop("_session_frame", None)
    label = kwargs.get("label", args[0] if args else "unnamed")
    marker = {"label": label, "preflight_passed": False, "tests_passed": False,
              "gate_passed": False, "learn_started": False,
              "defer_learn": True,
              "learn_phase": "validate", "learn_committed": False}
    timing_length = len(session_timings) if session_timings is not None else None
    prior_baseline = copy.deepcopy(
        getattr(getattr(session_frame, "code_context", None), "preflight_baseline", None)
    )
    session_state.checkpoint_in_progress = marker
    try:
        return _checkpoint_impl(anchor_fn, session_files_changed, session_state, *args,
                                _checkpoint_marker=marker, **kwargs)
    finally:
        # Nested actions are provisional evidence, not independent delivery
        # transitions.  The outer checkpoint records the sole final transition.
        if timing_length is not None:
            del session_timings[timing_length:]
        if session_frame is not None and hasattr(session_frame, "code_context"):
            session_frame.code_context.preflight_baseline = prior_baseline
        session_state.checkpoint_in_progress = None


def _checkpoint_impl(anchor_fn, session_files_changed, session_state, *args, **kwargs) -> dict | str:
    """Atomic save-point: preflight → test → gate → learning capture/assessment.

    Reduces between-feature ceremony from 4-5 manual commands to one.
    Assumes touched() was already called per-file (auto-chained by anchor("safe")).

    Args:
        anchor_fn: The anchor() dispatcher function (for recursive calls).
        session_files_changed: Mutable set of changed files.
        session_state: Mutable session state object.
        label: Required. Name for this checkpoint (e.g. "feature_name").
        skip_test: If True, skip test step (for doc-only checkpoints).
        test_target: Optional focused test target (file/pattern).
        learning_captures: Optional evidence-backed capture payloads.
        learning_assessment: Required when files changed. Explicit assessment payload.
        learn_events: Compatibility-only legacy payload; do not use for new callers.
        output_format: 'dict' or 'markdown'.

    Returns:
        Standard contract dict: kind="checkpoint", metrics={preflight_passed, tests_passed,
        tests_failed, gate_risk, learnings_saved}, summary="Checkpoint 'label': PASS/FAIL"
    """
    label = kwargs.pop("label", args[0] if args else "unnamed")
    skip_test = kwargs.pop("skip_test", False)
    test_target = kwargs.pop("test_target", None)
    learn_events = kwargs.pop("learn_events", None)
    learning_captures = kwargs.pop("learning_captures", None)
    learning_assessment = kwargs.pop("learning_assessment", None)
    learning_project_id = kwargs.pop(
        "_learning_project_id", getattr(session_state, "active_project", None),
    )
    output_format = kwargs.pop("output_format", "dict")
    final = kwargs.pop("final", False)
    generate_pr_draft = kwargs.pop("generate_pr_draft", None)
    checkpoint_marker = kwargs.pop("_checkpoint_marker")
    if type(final) is not bool:
        raise TypeError("final must be a bool")
    if generate_pr_draft is not None and type(generate_pr_draft) is not bool:
        raise TypeError("generate_pr_draft must be a bool or None")

    structured = learning_captures is not None or learning_assessment is not None
    if structured and learn_events is not None:
        raise RuntimeError("checkpoint accepts structured learning or compatibility learn_events, not both")
    if structured and not isinstance(learning_assessment, dict):
        raise RuntimeError("structured checkpoint requires learning_assessment")
    if learning_captures is None:
        learning_captures = []
    if not isinstance(learning_captures, list) or any(
        not isinstance(item, dict) for item in learning_captures
    ):
        raise TypeError("learning_captures must be a list of capture payload objects")
    if structured and learning_assessment.get("outcome") == "nothing_reusable_learned" and learning_captures:
        raise RuntimeError("nothing_reusable_learned forbids learning_captures")
    # Legacy payloads remain callable, but structured assessment is canonical.
    if session_files_changed and not learn_events and not structured:
        raise RuntimeError(
            "BLOCKED: anchor(\"checkpoint\") requires learning_assessment= when files changed.\n"
            "Use outcome='nothing_reusable_learned', or capture genuine observations "
            "and assess their IDs."
        )
    if not learn_events and not structured:
        structured = True
        learning_assessment = {"outcome": "nothing_reusable_learned"}

    # Validate skip_test: only allowed when no .py files were changed
    if skip_test and any(f.endswith(".py") for f in session_files_changed):
        raise RuntimeError(
            "BLOCKED: checkpoint(skip_test=True) is not allowed when .py files were changed.\n"
            f"Changed .py files: {[f for f in sorted(session_files_changed) if f.endswith('.py')]}\n"
            "Remove skip_test=True or use test_target= to focus the test run."
        )

    from odibi_anchor._utils.contract import build_base_context

    failed_at = None
    preflight_passed = False
    tests_passed = False
    tests_failed = 0
    tests_total = 0
    gate_risk = "unknown"
    learnings_saved = 0
    step_results = {}
    normalized_observation_ids: list[str] = []
    staged_pr = None

    # ── Step 1: Preflight (routed through anchor() for full enforcement) ──
    try:
        pf_result = anchor_fn("preflight", output_format="dict")
        step_results["preflight"] = pf_result
        pf_errors = pf_result.get("metrics", {}).get("errors", 1) if isinstance(pf_result, dict) else 1
        if pf_errors > 0:
            failed_at = "preflight"
            preflight_passed = False
        else:
            preflight_passed = True
            checkpoint_marker["preflight_passed"] = True
    except Exception as e:
        failed_at = "preflight"
        step_results["preflight"] = {"error": str(e)}

    # Short-circuit on preflight failure
    if failed_at == "preflight":
        summary = f"Checkpoint '{label}': FAIL at preflight"
        ctx = build_base_context(
            kind="checkpoint",
            subject=label,
            summary=summary,
            metrics={
                "preflight_passed": preflight_passed,
                "tests_passed": False,
                "tests_failed": 0,
                "gate_risk": "high",
                "learnings_saved": 0,
                "failed_at": "preflight",
            },
            findings=["Preflight failed — checkpoint aborted before test/gate."],
            risks=["Code has convention violations that must be fixed first."],
            samples={"preflight": step_results.get("preflight", {})},
            suggested_next_actions=[
                "MUST: Fix preflight errors, then re-run anchor('checkpoint', label='" + label + "')",
            ],
        )
        return format_checkpoint(ctx, output_format)

    # ── Step 2: Test (routed through anchor() for timing + target tracking) ──
    if not skip_test:
        try:
            test_kwargs = {"output_format": "dict"}
            if test_target:
                test_kwargs["target"] = test_target
            test_result = anchor_fn("test", **test_kwargs)
            step_results["test"] = test_result
            if isinstance(test_result, dict):
                tests_passed = test_result.get("metrics", {}).get("exit_code", 1) == 0
                tests_failed = test_result.get("metrics", {}).get("failed", 0)
                tests_total = (
                    test_result.get("metrics", {}).get("passed", 0)
                    + tests_failed
                    + test_result.get("metrics", {}).get("errors", 0)
                )
            else:
                tests_passed = False
        except Exception as e:
            step_results["test"] = {"error": str(e)}
            tests_passed = False
    else:
        tests_passed = True  # Skipped means "not blocking"
        step_results["test"] = {"skipped": True}
    checkpoint_marker["tests_passed"] = tests_passed
    if not tests_passed:
        failed_at = "test"

    # ── Step 3: Gate (routed through anchor() for drift, test cross-ref, evidence integrity) ──
    if failed_at is None:
        try:
            gate_result = anchor_fn("gate", output_format="dict")
            step_results["gate"] = gate_result
            if isinstance(gate_result, dict):
                gate_risk = gate_result.get("metrics", {}).get("risk_level", "unknown")
                if gate_risk in {"none", "low", "medium"}:
                    checkpoint_marker["gate_passed"] = True
                else:
                    failed_at = "gate"
            else:
                gate_risk = "unknown"
                failed_at = "gate"
        except Exception as e:
            step_results["gate"] = {"error": str(e)}
            gate_risk = "error"
            failed_at = "gate"

    # ── Step 4: checkpoint-owned activation and closure ──
    if failed_at is None:
        try:
            from odibi_anchor.codebase.structured_learning_context import (
                ensure_learning_obligation,
            )

            # Reuse the accepted task's cycle; create a new cycle only after a
            # prior checkpoint closed one for this same task.
            obligation = ensure_learning_obligation(
                task_window_id=session_state.task_window_id,
                session_ref="session:" + session_state.session_id.replace("-", ""),
                checkpoint_ref="checkpoint:" + label,
                project_id=learning_project_id,
            )
            checkpoint_marker["obligation_id"] = obligation["obligation_id"]
            session_state.learning_obligation_id = obligation["obligation_id"]
            checkpoint_marker["learn_started"] = True
            if structured:
                observation_ids = set(learning_assessment.get("observation_ids", []))
                capture_results = []
                for capture in learning_captures:
                    captured = anchor_fn("learning", "capture", output_format="dict", **capture)
                    capture_results.append(captured)
                    item = captured.get("item", {}) if isinstance(captured, dict) else {}
                    if item.get("item_id"):
                        observation_ids.add(item["item_id"])
                normalized_observation_ids = sorted(observation_ids)
                assessment_payload = dict(learning_assessment)
                if assessment_payload.get("outcome") == "observations_recorded":
                    assessment_payload["observation_ids"] = normalized_observation_ids
                step_results["learning_captures"] = capture_results
                checkpoint_marker["assessment_payload"] = assessment_payload
                learn_result = {"deferred": True}
            else:
                learn_result = anchor_fn("learn", session_events=learn_events, output_format="dict")
                step_results["learn"] = learn_result
                if isinstance(learn_result, dict):
                    learnings_saved = learn_result.get("metrics", {}).get("memories_added", 0)
                    if isinstance(learnings_saved, list):
                        learnings_saved = len(learnings_saved)
            if not isinstance(learn_result, dict):
                failed_at = "learn"
        except Exception as e:
            step_results["learn"] = {"error": str(e)}
            failed_at = "learn"

    # Phase 6 occurs only after every core step, including learn, has succeeded.
    pr_warning = None
    pr_path = None
    pr_status = None
    task_pr_request = getattr(session_state, "explicit_pr_draft_requested", None)
    if task_pr_request is not None and type(task_pr_request) is not bool:
        raise TypeError("stored generate_pr_draft request must be a bool or None")
    task_repository_baseline = getattr(session_state, "task_repository_baseline", None)
    databricks_git_folder = (
        getattr(task_repository_baseline, "evidence_kind", None) == "databricks_git_folder"
    )
    pr_requested = final or generate_pr_draft is True or task_pr_request is True
    if failed_at is None and pr_requested and databricks_git_folder:
        from odibi_anchor._pr_readiness import load_pr_config
        from odibi_anchor._repository_snapshot import (
            capture_task_change_scope,
            databricks_repository_capabilities,
        )

        artifact = getattr(session_state, "artifact_root", None)
        if not artifact:
            raise RuntimeError("checkpoint PR evaluation requires artifact_root")
        config = load_pr_config(artifact)
        explicit_required = generate_pr_draft is True or task_pr_request is True
        disposition = "required" if explicit_required or config["pr_readiness"] == "required" else (
            "not_required" if config["pr_readiness"] == "disabled" else "recommended"
        )
        required = disposition == "required"
        scope = capture_task_change_scope(
            task_repository_baseline,
            getattr(session_state, "task_repository_write_fingerprints", {}),
        )
        manual_actions = [
            "Inspect the complete Databricks Repos UI diff and identify pre-existing or unrelated changes.",
            "Confirm the intended task-scoped files and test evidence before committing.",
            "Commit and push manually in Databricks; Odibi Anchor will not call a Repos write API.",
            "Verify the remote PR, review, checks, and merge state separately.",
        ]
        message = (
            "Databricks Git Folder PR readiness is unavailable because local Git cleanliness, "
            "conflicts, merge-base/history, and complete Git diff evidence cannot be proven."
        )
        pr_status = {
            "disposition": disposition,
            "capability_status": "unavailable",
            "generated": False,
            "path": "unavailable",
            "ready": False,
            "head": scope.identity.head_sha,
            "target_ref": "unavailable",
            "target_sha": "unavailable",
            "merge_base_sha": "unavailable",
            "range": "unavailable",
            "conflict": "unavailable",
            "checks": [{
                "id": "repository.pr-readiness",
                "status": "unavailable",
                "description": message,
                "evidence_ids": [],
                "provenance": {"evidence_kind": "databricks_git_folder"},
            }],
            "evidence": [],
            "repository_capabilities": databricks_repository_capabilities(),
            "task_scoped_content_changes": {
                path: dict(change)
                for path, change in scope.provenance["content_changes"].items()
            },
            "manual_actions": manual_actions,
            "local_only": False,
            "external_actions": {
                "databricks_ui_diff_review_required": True,
                "manual_commit_push_required": True,
                "remote_pr_confirmation_required": True,
            },
            "generation_error": message,
        }
        step_results["pr_draft"] = {"error": message, "manual_actions": manual_actions}
        if required:
            failed_at = "pr_draft"
        else:
            pr_warning = message + " Manual Databricks delivery confirmation is required."
    if (failed_at is None and pr_requested and not databricks_git_folder):
        required = generate_pr_draft is True or task_pr_request is True
        try:
            from odibi_anchor._pr_readiness import (
                evaluate_pr_readiness,
                load_pr_config,
                pr_checks_to_evidence,
                render_pr_draft,
            )
            from odibi_anchor._repository_snapshot import capture_repository_snapshot
            from odibi_anchor.planning._task_policy import (
                ManagedArtifactEntry,
                evaluate_fresh_task_policies,
            )

            target = getattr(session_state, "target_root", None)
            artifact = getattr(session_state, "artifact_root", None)
            if not target or not artifact:
                raise RuntimeError("checkpoint PR generation requires target_root and artifact_root")
            config = load_pr_config(artifact)
            snapshot = capture_repository_snapshot(target, config["default_target_ref"],
                                                   artifact_root=artifact)
            profile = getattr(session_state, "active_task_profile", None)
            disposition = config["pr_readiness"]
            if profile is not None:
                _, policies = evaluate_fresh_task_policies(
                    profile, session_state=session_state, current_action="checkpoint",
                    current_effect="artifact_write", checkpoint_final=bool(final),
                    source_files_changed=tuple(sorted(session_files_changed)),
                    repository_snapshot=snapshot, repository_pr_config=config,
                )
                disposition = policies.pr_readiness.disposition
            required = required or disposition == "required"
            recommendation_suppressed = generate_pr_draft is False or task_pr_request is False
            should_generate = required or (disposition == "recommended" and not recommendation_suppressed)
            pr_status = {"disposition": disposition, "generated": False, "path": None,
                         "ready": False, "head": snapshot.head_sha,
                         "target_ref": snapshot.configured_target_ref,
                         "target_sha": snapshot.target_sha, "merge_base_sha": snapshot.merge_base_sha,
                         "range": snapshot.committed_diff_range,
                         "conflict": snapshot.local_conflict_result, "checks": [],
                         "evidence": [], "local_only": True, "external_actions": False}
            if should_generate:
                attestations = tuple(getattr(session_state, "guidance_attestations", ()))
                readiness = evaluate_pr_readiness(
                    snapshot, config, attestations=attestations,
                    intended_pr_paths=tuple(getattr(session_state, "intended_pr_paths", ())),
                )
                mechanical_evidence = pr_checks_to_evidence(readiness)
                pr_status.update({
                    "ready": readiness.ready,
                    "checks": [{"id": item.id, "status": item.status,
                                "description": item.description,
                                "evidence_ids": list(item.evidence_ids),
                                "provenance": dict(item.provenance)}
                               for item in readiness.checks],
                    "evidence": sorted({evidence for item in readiness.checks
                                        for evidence in item.evidence_ids}),
                    "external_actions": dict(readiness.external_actions),
                })
                title_prefix = config["title_prefixes"][0]
                staging = tempfile.TemporaryDirectory(prefix="anchor-pr-stage-")
                staged_root = Path(staging.name)
                path = render_pr_draft(staged_root, snapshot, readiness,
                                       title=f"{title_prefix}: describe local changes",
                                       title_prefixes=tuple(config["title_prefixes"]))
                relative = path.relative_to(staged_root)
                destination = Path(artifact).resolve() / relative
                if destination.exists():
                    path.unlink()
                    path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(destination, path)
                    path = render_pr_draft(staged_root, snapshot, readiness,
                                           title=f"{title_prefix}: describe local changes",
                                           title_prefixes=tuple(config["title_prefixes"]))
                staged_pr = (staging, path, destination, mechanical_evidence, ManagedArtifactEntry,
                             hashlib.sha256(path.read_bytes()).hexdigest())
                pr_path = str(destination)
                step_results["pr_draft"] = {"path": pr_path, "ready": readiness.ready}
                pr_status.update({"generated": False, "path": None})
            # Policy inputs become current only after required rendering has succeeded.
            session_state.repository_snapshot = snapshot
            session_state.repository_pr_config = config
        except Exception as exc:
            step_results["pr_draft"] = {"error": str(exc)}
            if pr_status is not None:
                pr_status["generation_error"] = str(exc)
            if required:
                failed_at = "pr_draft"
            else:
                pr_warning = f"Recommended PR draft was not generated: {exc}"

    # A final checkpoint may legitimately suppress or skip recommended generation.
    # Its validated learning is committed here before the checkpoint transition.
    if (failed_at is None and checkpoint_marker.get("defer_learn") and
            not checkpoint_marker.get("learn_committed")):
        try:
            checkpoint_marker["learn_phase"] = "commit"
            if structured:
                committed_learn = anchor_fn(
                    "learning", "assess", output_format="dict",
                    **checkpoint_marker["assessment_payload"],
                )
                step_results["assessment"] = committed_learn
            else:
                committed_learn = anchor_fn("learn", session_events=learn_events, output_format="dict")
            if not isinstance(committed_learn, dict):
                raise RuntimeError("checkpoint learning did not return structured evidence")
            from odibi_anchor.codebase.structured_learning_context import learning_obligation
            terminal = learning_obligation(
                checkpoint_marker["obligation_id"],
                project_id=learning_project_id,
                task_window_id=session_state.task_window_id,
            )
            if terminal is None or terminal.get("status") not in {"assessed", "legacy_closed"}:
                raise RuntimeError(
                    "checkpoint learning obligation did not reach its required terminal status"
                )
            checkpoint_marker["learn_committed"] = True
            if not structured:
                step_results["learn"] = committed_learn
                learnings_saved = committed_learn.get("metrics", {}).get("memories_added", 0)
                if isinstance(learnings_saved, list):
                    learnings_saved = len(learnings_saved)
        except Exception as exc:
            step_results["learn"] = {"error": str(exc)}
            failed_at = "learn"

    # The draft was fully rendered and validated off-ledger. Closure is now exact;
    # publish bytes and both in-memory ledgers as one compensated transition.
    if failed_at is None and staged_pr is not None:
        staging, staged_path, destination, mechanical_evidence, artifact_type, sha256 = staged_pr
        evidence_start = len(session_state.evidence_ledger)
        artifact_start = len(session_state.managed_artifact_ledger)
        prior = destination.read_bytes() if destination.exists() else None
        try:
            persisted_snapshot = capture_repository_snapshot(
                target, config["default_target_ref"], artifact_root=artifact,
            )
            if (persisted_snapshot.head_sha != snapshot.head_sha or
                    persisted_snapshot.target_sha != snapshot.target_sha or
                    persisted_snapshot.provenance["worktree_fingerprint"] !=
                    snapshot.provenance["worktree_fingerprint"]):
                raise RuntimeError("repository source changed while publishing PR draft")
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staged_path, destination)
            session_state.evidence_ledger.extend(mechanical_evidence)
            session_state.managed_artifact_ledger.append(artifact_type(
                str(destination), "pr_draft", datetime.now(timezone.utc).isoformat(),
                {"snapshot_head": snapshot.head_sha, "snapshot_target": snapshot.target_sha,
                 "range": snapshot.committed_diff_range, "sha256": sha256,
                 "evidence_ids": tuple(item.id for item in mechanical_evidence),
                 "scope": "local_only", "external_actions": False},
            ))
            session_state.repository_snapshot = persisted_snapshot
            pr_status.update({"generated": True, "path": str(destination)})
        except Exception as exc:
            del session_state.evidence_ledger[evidence_start:]
            del session_state.managed_artifact_ledger[artifact_start:]
            if prior is None:
                destination.unlink(missing_ok=True)
            else:
                rollback = destination.with_name(".PR_DRAFT.rollback.tmp")
                rollback.write_bytes(prior)
                os.replace(rollback, destination)
            # SQLite closure is authoritative. Only external bytes and the
            # in-memory publication ledgers are compensated; boot reconciles it.
            failed_at = "pr_draft"
            step_results["pr_draft"] = {"error": str(exc)}
            pr_status["generation_error"] = str(exc)
        finally:
            staging.cleanup()

    # ── Build final result ──
    overall_pass = preflight_passed and tests_passed and failed_at is None
    # Reset checkpoint file counter on success so next feature gets a fresh budget
    if overall_pass:
        session_state.files_at_last_checkpoint = len(session_files_changed)
    summary = f"Checkpoint '{label}': {'PASS' if overall_pass else 'FAIL at ' + (failed_at or 'test')}"
    if (
        overall_pass
        and isinstance(pr_status, dict)
        and pr_status.get("capability_status") == "unavailable"
    ):
        summary += " — manual Databricks delivery required"

    findings = []
    if preflight_passed:
        findings.append("✓ Preflight passed")
    if skip_test:
        findings.append("○ Tests skipped (skip_test=True)")
    elif tests_passed:
        findings.append(f"✓ Tests passed ({tests_total} total)")
    else:
        findings.append(f"✗ Tests failed ({tests_failed} failures)")
    findings.append(f"{'✓' if gate_risk in ('none', 'low', 'medium') else '✗'} Gate risk: {gate_risk}")
    if learnings_saved:
        findings.append(f"✓ {learnings_saved} learning(s) saved")

    ctx = build_base_context(
        kind="checkpoint",
        subject=label,
        summary=summary,
        metrics={
            "preflight_passed": preflight_passed,
            "tests_passed": tests_passed,
            "tests_failed": tests_failed,
            "gate_risk": gate_risk,
            "learnings_saved": learnings_saved,
            "closure_route": "structured" if structured else "legacy",
            "assessment_id": (
                step_results.get("assessment", {}).get("assessment", {}).get("assessment_id")
                if structured else None
            ),
            "assessment_outcome": learning_assessment.get("outcome") if structured else None,
            "observations_recorded": (
                normalized_observation_ids
                if structured else []
            ),
            "failed_at": failed_at,
            "overall_pass": overall_pass,
            "core_steps_succeeded": failed_at in {None, "pr_draft"},
            "pr_draft_path": pr_path,
        },
        findings=findings,
        risks=([f"Checkpoint failed at {failed_at}"] if failed_at else []) + ([pr_warning] if pr_warning else []),
        samples={"pr_readiness": pr_status} if pr_status is not None else {},
        suggested_next_actions=(
            (
                list(pr_status.get("manual_actions", ()))
                if isinstance(pr_status, dict) and pr_status.get("capability_status") == "unavailable"
                else ["Ready for next feature or delivery."]
            ) if overall_pass
            else [f"MUST: Fix {failed_at or 'test'} failures, then re-run anchor('checkpoint', label='{label}')"]
        ),
    )
    return format_checkpoint(ctx, output_format)


def format_checkpoint(result: dict, output_format: str) -> dict | str:
    """Format checkpoint result as dict or markdown."""
    if output_format == "markdown":
        lines = [f"# Checkpoint: {result['subject']}\n"]
        lines.append(f"**{result['summary']}**\n")
        lines.append("## Metrics\n")
        lines.append("| Metric | Value |")
        lines.append("| --- | --- |")
        for k, v in result["metrics"].items():
            lines.append(f"| {k} | {v} |")
        if result["findings"]:
            lines.append("\n## Steps\n")
            for f in result["findings"]:
                lines.append(f"- {f}")
        if result["risks"]:
            lines.append("\n## Risks\n")
            for r in result["risks"]:
                lines.append(f"- {r}")
        if result.get("suggested_next_actions"):
            lines.append("\n## Next Actions\n")
            for a in result["suggested_next_actions"]:
                lines.append(f"- {a}")
        pr = result.get("samples", {}).get("pr_readiness")
        if pr:
            lines.extend(["\n## PR readiness\n",
                          f"- disposition/generated/path: {pr['disposition']} / {pr['generated']} / {pr['path']}",
                          f"- ready/head/range: {pr['ready']} / {pr['head']} / {pr['range']}",
                          f"- checks/evidence: {len(pr['checks'])} / {len(pr['evidence'])}",
                          f"- local-only/external actions: {pr['local_only']} / {pr['external_actions']}"])
            for check in pr["checks"]:
                lines.append(
                    f"- [{check['status']}] {check['id']}: {check['description']} "
                    f"(evidence: {', '.join(check['evidence_ids']) or 'none'})"
                )
            if pr.get("generation_error"):
                lines.append(f"- generation error: {pr['generation_error']}")
        return "\n".join(lines)
    return result
