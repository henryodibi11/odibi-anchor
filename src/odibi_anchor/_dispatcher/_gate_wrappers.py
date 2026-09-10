"""Gate wrapper closures — extracted from anchor() in agent_init.py.

Contains the preflight-with-baseline wrapper, test coverage check,
and the gate-with-auto-confirm mega-closure.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any


def preflight_with_baseline(
    root, kwargs, *,
    session_frame, frame_enabled, session_files_changed, preflight_fn,
    session_state=None,
):
    """anchor("preflight") with baseline injection/capture from context frame."""
    # Handle reset_baseline kwarg
    _reset = kwargs.pop("reset_baseline", False)
    if _reset and frame_enabled and session_frame:
        session_frame.code_context.preflight_baseline = None

    # Inject baseline from frame
    if "baseline" not in kwargs and frame_enabled and session_frame:
        kwargs["baseline"] = session_frame.code_context.preflight_baseline

    task_scope = None
    baseline = getattr(session_state, "task_repository_baseline", None)
    if baseline is not None:
        from odibi_anchor._repository_snapshot import capture_task_change_scope
        task_scope = capture_task_change_scope(
            baseline, getattr(session_state, "task_repository_write_fingerprints", {}),
        )
    # Inject changed_files default from Git scope; ledger remains provenance only.
    if "changed_files" not in kwargs:
        kwargs["changed_files"] = sorted(
            task_scope.changed_paths if task_scope is not None else session_files_changed
        )
    if task_scope is not None:
        explicit = kwargs.get("changed_files")
        if explicit is not None and set(explicit) != set(task_scope.changed_paths):
            raise RuntimeError("explicit preflight changed_files must equal the full task scope")
        kwargs["changed_line_ranges"] = task_scope.changed_line_ranges
        kwargs["created_paths"] = tuple(task_scope.provenance.get("created_paths", ()))

    _pf_result = preflight_fn(root, **kwargs)
    if isinstance(_pf_result, dict) and task_scope is not None:
        if getattr(task_scope, "evidence_kind", None) == "databricks_git_folder":
            _pf_result.setdefault("metrics", {}).update({
                "scope_source": "databricks_git_folder",
                "host_head_start": task_scope.provenance["host_head_start"],
                "host_head_current": task_scope.provenance["host_head_current"],
                "host_identity_stability": task_scope.provenance["host_identity_stability"],
                "repository_capabilities": dict(task_scope.provenance["capabilities"]),
            })
        else:
            _pf_result.setdefault("metrics", {}).update({
                key: task_scope.provenance.get(key)
                for key in ("target_start_sha", "target_current_sha", "target_drift")
            })

    # Capture baseline on first run (stored as sorted list for JSON safety)
    if isinstance(_pf_result, dict):
        _captured = _pf_result.get("metrics", {}).pop("_captured_baseline", None)
        if _captured and frame_enabled and session_frame:
            session_frame.code_context.preflight_baseline = set(_captured)

    return _pf_result


def check_test_coverage(changed_py: list, test_target: str, frame=None) -> bool:
    """Multi-strategy test coverage check — delegates to extracted pure function.

    Returns True if the test_target plausibly covers the changed files.
    """
    from odibi_anchor._dispatcher._enforcement import check_test_coverage as _ctc

    # Extract test_map from frame if available
    test_map: dict = {}
    if frame and hasattr(frame, "code_context") and frame.code_context.codebase_map:
        test_map = frame.code_context.codebase_map.get("test_map", {})

    is_covered, _ = _ctc(changed_py, test_target, test_map=test_map or None)
    return is_covered


def _attach_assurance_shadow(
    result, *, session_state, session_timings, deferred: bool,
    task_scope=None, unattested_paths=(),
):
    """Add advisory assurance output after the legacy gate decision is complete."""
    if not isinstance(result, dict):
        return result
    plan = getattr(session_state, "active_assurance_plan", None)
    if plan is None:
        return result
    try:
        from odibi_anchor.assurance import (
            T1_CONTROLS,
            CommandResultEvidence,
            assurance_shadow_metrics,
            build_assurance_plan,
            evaluate_assurance,
            evaluate_overlay_controls,
            evaluate_t1_controls,
            normalize_assurance_evidence,
            overlay_input_from_profile,
        )
        from odibi_anchor.planning._task_policy import build_task_policy_context

        profile = getattr(session_state, "active_task_profile", None)
        if profile is None or plan != build_assurance_plan(profile):
            raise ValueError("the accepted assurance plan does not match the active TaskProfile")
        context = build_task_policy_context(
            profile,
            session_state=session_state,
            current_action="gate",
        )
        assessment = evaluate_assurance(
            plan,
            normalize_assurance_evidence(context, session_timings),
        )
        projection = assurance_shadow_metrics(assessment)
        result.setdefault("metrics", {})["assurance_shadow"] = projection
        unresolved = [
            item for item in assessment.results
            if item.applicability == "applicable" and item.evidence_state != "satisfied"
        ]
        if unresolved:
            summary = ", ".join(
                f"{item.control_id}={item.evidence_state}" for item in unresolved
            )
            result.setdefault("findings", []).append(
                f"SHADOW: advisory assurance evidence is incomplete ({summary})."
            )

        changed_paths = tuple(sorted(getattr(task_scope, "changed_paths", ())))
        provenance = getattr(task_scope, "provenance", {})
        current_sha = (
            provenance.get("target_current_sha") or provenance.get("host_head_current")
            if isinstance(provenance, Mapping) else None
        )
        subject_digest = "sha256:" + hashlib.sha256(
            f"git:{current_sha or 'unavailable'}".encode()
        ).hexdigest()
        scope_payload = {
            "changed_paths": list(changed_paths),
            "changed_line_ranges": [
                {
                    "path": item.path, "kind": item.kind,
                    "start": item.start, "end": item.end,
                }
                for item in getattr(task_scope, "changed_line_ranges", ())
            ],
        }
        scope_digest = "sha256:" + hashlib.sha256(json.dumps(
            scope_payload, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        trusted_evidence: list[Any] = list(
            getattr(session_state, "assurance_result_evidence", ())
        )
        intended = tuple(sorted(getattr(session_state, "intended_pr_paths", ())))
        timestamp = datetime.now(UTC).isoformat()
        trusted_evidence.append(CommandResultEvidence(
            evidence_id="gate:scope-integrity",
            check_id="scope-integrity",
            state="satisfied" if task_scope is not None else "unavailable",
            observed_at=timestamp,
            completed_at=timestamp,
            subject_digest=subject_digest,
            scope_digest=scope_digest,
            producer="odibi-anchor-gate",
            producer_version="1",
            result={
                "intended_paths_known": bool(intended),
                "reconciled": bool(intended) and set(intended) == set(changed_paths),
                "denied_paths": [],
                "unattested_paths": list(unattested_paths),
            },
            provenance={"scope_source": "task_repository_baseline"},
        ))
        t1_controls = getattr(session_state, "assurance_t1_controls", T1_CONTROLS)
        t1_results = evaluate_t1_controls(
            changed_paths,
            trusted_evidence,
            subject_digest=subject_digest,
            scope_digest=scope_digest,
            implementation_delivery=(
                getattr(profile, "execution_mode", None) == "source_change"
            ),
            controls=t1_controls,
        )
        result.setdefault("metrics", {})["t1_assurance"] = {
            "mode": "advisory",
            "subject_digest": subject_digest,
            "scope_digest": scope_digest,
            "results": [item.to_dict() for item in t1_results],
        }
        overlay_selection, overlay_results = evaluate_overlay_controls(
            overlay_input_from_profile(profile, plan),
            trusted_evidence,
            subject_digest=subject_digest,
            scope_digest=scope_digest,
        )
        if overlay_selection.selected:
            result.setdefault("metrics", {})["assurance_overlays"] = {
                "mode": "shadow",
                "catalog_version": overlay_selection.catalog_version,
                "catalog_digest": overlay_selection.catalog_digest,
                "selected": [item.to_dict() for item in overlay_selection.selected],
                "results": [item.to_dict() for item in overlay_results],
            }
        blocking = [
            item for item in t1_results
            if item.applicability == "applicable"
            and item.disposition == "blocking"
            and item.evidence_state != "satisfied"
        ]
        if blocking:
            obligations = result.setdefault("obligations", [])
            for item in blocking:
                obligations.append({
                    "tool": item.control_id,
                    "reason": f"result-backed control is {item.evidence_state}",
                    "priority": "MUST",
                    "created_by_action": "assurance",
                    "suggested_call": "run the required canonical verification and retain its normalized result",
                })
            metrics = result.setdefault("metrics", {})
            metrics["must_unpaid"] = int(metrics.get("must_unpaid", 0)) + len(blocking)
            metrics["obligations_owed"] = int(metrics.get("obligations_owed", 0)) + len(blocking)
            metrics["all_verified"] = False
            result["learn_reminder"] = None
            if result.get("passed") is True:
                result["passed"] = False
            if result.get("overall_pass") is True:
                result["overall_pass"] = False
            if result.get("status") in {"pass", "passed"}:
                result["status"] = "fail"
            if result.get("exit_status") == 0:
                result["exit_status"] = 1
            result["summary"] = (
                f"{len(blocking)} blocking result-backed assurance control(s) unresolved. "
                + str(result.get("summary", ""))
            )
    except Exception as exc:
        diagnostic = f"gate-refresh:{type(exc).__name__}:{str(exc)[:240]}"
        result.setdefault("metrics", {})["assurance_shadow"] = {
            "mode": "shadow",
            "status": "degraded",
            "diagnostics": [diagnostic],
        }
        result.setdefault("findings", []).append(
            "SHADOW: assurance evaluation degraded; the legacy gate result remains authoritative."
        )
        from odibi_anchor._utils._session_state import record_degraded
        record_degraded("assurance_shadow_gate", diagnostic)
    return result


def gate_with_auto_confirm(
    root, args, kwargs, *,
    session_timings, session_files_changed, session_files_created,
    session_frame, frame_enabled, session_state,
    workflow_gate_fn,
    check_drift_fn, check_test_coverage_fn, reconcile_ledger_fn=None, memory_db=None,
):
    """anchor("gate") with enforcement checks and evidence derivation.

    The private deferred mode is used only by checkpoint. It evaluates the same
    rules but leaves all successful-gate bookkeeping untouched until checkpoint's
    final success transition.
    """
    _reconciled = (
        reconcile_ledger_fn(root) if reconcile_ledger_fn is not None
        else {"removed_created": [], "removed_restored": []}
    )
    if "_deferred" in kwargs or "no_commit" in kwargs or "_checkpoint_capability" in kwargs:
        raise ValueError("deferred gate mode is internal-only")
    # The enclosing checkpoint marker is closure-owned dispatcher state.  Unlike a
    # keyword token it cannot be imported and replayed by a public caller.
    _deferred = getattr(session_state, "checkpoint_in_progress", None) is not None
    _epoch = getattr(session_state, "task_verification_epoch", None)
    verification_timings = session_timings[_epoch:] if _epoch is not None else session_timings
    _current_task_timing = None
    if type(_epoch) is int and 0 < _epoch <= len(session_timings):
        _candidate = session_timings[_epoch - 1]
        if (
            _candidate.get("action") == "task"
            and _candidate.get("error") is None
            and _candidate.get("passed") is True
        ):
            _current_task_timing = _candidate
    task_scope = None
    task_baseline = getattr(session_state, "task_repository_baseline", None)
    if task_baseline is not None:
        from odibi_anchor._repository_snapshot import capture_task_change_scope
        task_scope = capture_task_change_scope(
            task_baseline, getattr(session_state, "task_repository_write_fingerprints", {}),
        )
    scope_files = set(task_scope.changed_paths) if task_scope is not None else set(session_files_changed)
    scope_created = (set(task_scope.provenance.get("created_paths", ()))
                     if task_scope is not None else set(session_files_created))
    if (
        memory_db is not None
        and task_scope is not None
        and task_scope.provenance.get("baseline_authority") == "adopted"
    ):
        from odibi_anchor.codebase._adopted_dirty import adoption_status

        adoption = task_scope.provenance.get("adoption") or {}
        status = adoption_status(memory_db, adoption_id=adoption.get("adoption_id", ""))
        if status["status"] != "active":
            raise RuntimeError(
                "BLOCKED: adopted task authority was withdrawn; retained provenance cannot "
                "authorize this gate"
            )
    # ── Enforce learn after previous SUCCESSFUL gate ──
    from odibi_anchor._dispatcher._enforcement import should_block_gate_learn as _sbgl
    _blocked, _block_msg = _sbgl(verification_timings)
    if _blocked:
        from odibi_anchor._dispatcher._blocked_action import BlockedActionError
        raise BlockedActionError(
            f"BLOCKED: {_block_msg}\n"
            "Run anchor(\"learning\", \"capture\", ...) when reusable facts exist, then "
            "anchor(\"learning\", \"assess\", outcome=...) and re-run gate.",
            required_action="learning", resume_action="gate",
        )

    # ── Enforce self-review: require anchor("review") or anchor("diff") before gate ──
    from odibi_anchor._dispatcher._enforcement import should_block_gate_review as _sbgr
    _review_blocked, _review_block_msg = _sbgr(verification_timings, scope_files)
    if _review_blocked:
        from odibi_anchor._dispatcher._blocked_action import BlockedActionError
        raise BlockedActionError(
            f"BLOCKED: anchor(\"gate\") — {_review_block_msg}\n"
            "Review your changeset before gating: anchor(\"review\")",
            required_action="review", resume_action="gate",
        )

    # ── Enforce test pass: if tests ran and failed, block gate ──
    from odibi_anchor._dispatcher._enforcement import should_block_gate_tests as _sbgt
    _test_blocked, _test_block_msg = _sbgt(verification_timings, scope_files)
    if _test_blocked:
        from odibi_anchor._dispatcher._blocked_action import BlockedActionError
        raise BlockedActionError(
            f"BLOCKED: anchor(\"gate\") — {_test_block_msg}\n"
            "Run anchor(\"test\") first.", required_action="test", resume_action="gate",
        )

    # ── Enforce preflight: require anchor("preflight") for .py changes before gate ──
    from odibi_anchor._dispatcher._enforcement import should_block_gate_preflight as _sbgp
    _pf_blocked, _pf_block_msg = _sbgp(verification_timings, scope_files)
    if _pf_blocked:
        from odibi_anchor._dispatcher._blocked_action import BlockedActionError
        raise BlockedActionError(
            f"BLOCKED: anchor(\"gate\") — {_pf_block_msg}\n"
            "Preflight catches convention/import breakage before delivery.",
            required_action="preflight", resume_action="gate",
        )

    # Need test entries for coverage check below
    _py_changed = any(f.endswith(".py") for f in scope_files)
    if _py_changed:
        _test_entries = [
            t for t in verification_timings
            if t["action"] == "test" and t.get("error") is None
        ]
        _last_test = _test_entries[-1] if _test_entries else None
        # Cross-reference: focused test target must cover changed files
        _last_test_target = _last_test.get("test_target") if _last_test else None
        if _last_test and _last_test.get("test_mark"):
            raise RuntimeError(
                "BLOCKED: anchor(\"gate\") — the latest passing test run used marker filter "
                f"'{_last_test['test_mark']}', so it cannot prove general coverage for "
                "changed Python files. Run the applicable tests again without mark=."
            )
        if _last_test_target:
            _changed_py = [f for f in scope_files if f.endswith(".py")]
            _test_covers_changed = check_test_coverage_fn(
                changed_py=_changed_py,
                test_target=_last_test_target,
                frame=session_frame if frame_enabled else None,
            )
            if not _test_covers_changed:
                raise RuntimeError(
                    f"BLOCKED: anchor(\"gate\") — test target '{_last_test_target}' doesn't cover "
                    f"changed .py files: {_changed_py[:5]}.\n"
                    "Run anchor(\"test\", target=\"tests/\") for the full suite, "
                    "or target tests that cover the changed files."
                )


    # ── Enforce output evidence: data-mode delivery requires a verified output (D-010) ──
    from odibi_anchor._dispatcher._enforcement import (
        should_block_gate_outcome_evidence as _sboe,
    )
    from odibi_anchor.planning._task_builders import _DATA_SPEC_MODES as _DATA_MODES
    _oe_blocked, _oe_block_msg = _sboe(
        getattr(session_state, "active_task_mode", None), verification_timings, _DATA_MODES,
    )
    if _oe_blocked:
        raise RuntimeError(
            f"BLOCKED: anchor(\"gate\") — {_oe_block_msg}\n"
            "Shift-left: a config that only parses is not a pipeline that ran. "
            "Prove the output exists and has the right grain/row-count before delivery."
        )

    # ── Filesystem drift detection: auto-register untracked changes ──
    # Auto-register unregistered changes (fixes the #1 compliance gap: touched
    # misses). Hardening (H-003): unregistered .py changes mean a file was edited
    # outside the gated path (e.g. editAsset), so the pre-edit known_bad gate
    # never fired. Rather than blanket-blocking every editAsset session, run the
    # known_bad guardrail on the drifted .py files NOW and block ONLY if it
    # returns a high-confidence "block" (a confirmed known-bad pattern is being
    # repeated). Clean drift is auto-touched and surfaced as a finding.
    _databricks_task = getattr(task_baseline, "evidence_kind", None) == "databricks_git_folder"
    _auto_touched_files = []
    _unattested_outside_scope = []
    _drift_known_bad_status = None
    try:
        _drift = check_drift_fn(str(root))
        if _drift.get("has_drift"):
            _unreg = _drift["unregistered"]
            if _databricks_task:
                in_scope = sorted(set(_unreg) & scope_files)
                if in_scope:
                    raise RuntimeError(
                        "BLOCKED: Databricks Git Folder gate detected unacknowledged in-scope "
                        "filesystem drift: " + ", ".join(in_scope[:8])
                    )
                _unattested_outside_scope = sorted(
                    set(_unreg) | (set(_drift.get("deleted", ())) - scope_files)
                )
                _unreg = []
            from odibi_anchor._dispatcher._enforcement import should_block_documentation_paths
            _doc_blocked, _doc_message = should_block_documentation_paths(
                getattr(session_state, "active_task_mode", None), _unreg,
            )
            if _doc_blocked:
                raise RuntimeError(f"BLOCKED: {_doc_message}")
            _unreg_py = [p for p in _unreg if p.endswith(".py")]
            _known_bad_ran = any(
                t["action"] == "known_bad" and t.get("error") is None
                for t in session_timings
            )
            if _unreg_py and not _known_bad_ran:
                from odibi_anchor.codebase.known_bad_change_context import (
                    known_bad_change_context as _kb_check,
                )
                _kb = _kb_check(
                    str(root),
                    changed_files=_unreg_py,
                    output_format="dict",
                    db_path=kwargs.get("db_path"),
                )
                _drift_known_bad_status = (
                    _kb.get("metrics", {}).get("status") if isinstance(_kb, dict) else None
                )
                if _drift_known_bad_status == "block":
                    _kb_risks = _kb.get("risks", []) if isinstance(_kb, dict) else []
                    raise RuntimeError(
                        "BLOCKED: anchor(\"gate\") — a file edited outside "
                        "anchor(\"safe\")/anchor(\"touched\") matches a high-confidence "
                        "known-bad pattern:\n  "
                        + "\n  ".join(_unreg_py[:8])
                        + "\nKnown-bad findings:\n  "
                        + "\n  ".join(str(r) for r in _kb_risks[:3])
                        + "\n\nTo unblock:\n"
                        "  1. Revise the change to avoid the pattern above.\n"
                        f"  2. Run anchor(\"known_bad\", changed_files={_unreg_py!r}).\n"
                        "  3. Run anchor(\"touched\", \"<path>\") for each file.\n"
                        "  4. Re-run anchor(\"gate\")."
                    )
            from odibi_anchor._utils._session_state import touched as _auto_touch
            for _path in _unreg:
                _auto_touch(_path, created=(_path in _drift.get("created", [])), root=str(root))
                _auto_touched_files.append(_path)
            # Update session_files_changed so gate sees them
            session_files_changed.update(_unreg)
    except RuntimeError:
        raise  # H-003 block must not be swallowed by the drift-safety catch
    except Exception as _exc:
        from odibi_anchor._utils._session_state import record_degraded
        record_degraded("gate_drift_detection", _exc)

    # Re-check the complete final ledger after drift discovery. This catches
    # both explicitly registered and out-of-band changes before delivery.
    from odibi_anchor._dispatcher._enforcement import should_block_documentation_paths
    _doc_blocked, _doc_message = should_block_documentation_paths(
        getattr(session_state, "active_task_mode", None), scope_files,
    )
    if _doc_blocked:
        raise RuntimeError(f"BLOCKED: {_doc_message}")

    # Drift detection may have discovered files that were absent during
    # pre-dispatch. Reject read-only task modes before gate state can advance.
    from odibi_anchor._dispatcher._enforcement import should_block_mode_mismatch
    _blocked, _message = should_block_mode_mismatch(
        verification_timings, scope_files,
    )
    if _blocked:
        raise RuntimeError(f"BLOCKED: {_message}")

    # ── Derive all gate evidence from session state — never trust caller kwargs ──
    # Strip bypass kwargs that would let the agent fabricate evidence
    for _unsafe_key in ("actions_taken", "files_changed", "files_created",
                        "obligations_paid", "verification_record",
                        "skip_timing_verification", "_timings_override"):
        kwargs.pop(_unsafe_key, None)

    # Build actions_taken from session timings
    _gate_actions = []
    _actions_seen = set()
    for _t in verification_timings:
        if _t.get("error") is None and _t["action"] not in _actions_seen:
            _actions_seen.add(_t["action"])
            _gate_actions.append(_t["action"])

    # Build obligations_paid from session timings (tools that actually ran successfully)
    _obligation_tool_map = {
        "task": "task_execution_context",
        "preflight": "preflight_context",
        "convention": "convention_preflight_context",
        "test": "test_focus_context",
        "consistency": "consistency_check_context",
        "impact": "change_impact_context",
        "known_bad": "known_bad_change_context",
        "dogfood": "dogfood_regression_context",
    }
    _obligation_timings = [
        timing for timing in verification_timings
        if timing.get("action") != "task"
    ]
    if _current_task_timing is not None:
        _obligation_timings.insert(0, _current_task_timing)
    _paid = []
    for _t in _obligation_timings:
        if _t.get("error") is None and _t["action"] in _obligation_tool_map:
            _tool_name = _obligation_tool_map[_t["action"]]
            if _tool_name not in _paid:
                _paid.append(_tool_name)

    kwargs["actions_taken"] = _gate_actions
    kwargs["files_changed"] = sorted(scope_files)
    kwargs["files_created"] = sorted(scope_created)
    kwargs["obligations_paid"] = _paid

    result = workflow_gate_fn(root, *args, **kwargs)
    if isinstance(result, dict) and task_scope is not None:
        if _databricks_task:
            # Re-attest after gate execution so the returned evidence is final-bound.
            from odibi_anchor._repository_snapshot import capture_task_change_scope
            task_scope = capture_task_change_scope(
                task_baseline,
                getattr(session_state, "task_repository_write_fingerprints", {}),
            )
            result.setdefault("metrics", {}).update({
                "scope_source": "databricks_git_folder",
                "host_head_start": task_scope.provenance["host_head_start"],
                "host_head_current": task_scope.provenance["host_head_current"],
                "host_identity_stability": task_scope.provenance["host_identity_stability"],
                "repository_capabilities": dict(task_scope.provenance["capabilities"]),
                "task_scoped_content_change_count": len(task_scope.changed_paths),
            })
            result.setdefault("samples", {})["task_scoped_content_changes"] = {
                path: dict(change)
                for path, change in task_scope.provenance["content_changes"].items()
            }
            result.setdefault("risks", []).extend([
                "Databricks Repos does not expose pre-existing working-tree/index dirtiness, "
                "Git conflicts, merge-base/history, or PR readiness.",
                "Changes outside repository_scope are not attested by this gate; inspect the "
                "complete Databricks Repos UI diff before delivery.",
            ])
            result.setdefault("suggested_next_actions", []).extend([
                "MUST: In Databricks Repos UI, inspect the complete diff and confirm no "
                "pre-existing or unrelated changes are included.",
                "MUST: Commit and push manually in Databricks, then verify the remote PR and "
                "merge state outside Odibi Anchor.",
            ])
        else:
            result.setdefault("metrics", {}).update({
                key: task_scope.provenance.get(key)
                for key in ("target_start_sha", "target_current_sha", "target_drift")
            })
            if task_scope.provenance.get("baseline_authority") == "adopted":
                adoption = dict(task_scope.provenance.get("adoption") or {})
                result.setdefault("metrics", {})["baseline_authority"] = "adopted"
                result.setdefault("samples", {})["adoption_provenance"] = adoption
                result.setdefault("findings", []).append(
                    "Gate evidence covers the complete original diff from the prior accepted "
                    "task's task-start baseline under adopted authority."
                )

    if isinstance(result, dict) and any(_reconciled.values()):
        result.setdefault("metrics", {})["reconciled_file_count"] = sum(
            len(paths) for paths in _reconciled.values()
        )
        result.setdefault("findings", []).append(
            "Removed proven net-zero session changes before gate: "
            f"created-then-deleted={_reconciled['removed_created']}, "
            f"restored={_reconciled['removed_restored']}"
        )

    # Inject auto-touched metadata into gate output for transparency
    if _auto_touched_files and isinstance(result, dict):
        result.setdefault("metrics", {})["auto_touched_count"] = len(_auto_touched_files)
        _msg = (
            f"Auto-detected {len(_auto_touched_files)} untracked file changes: "
            f"{_auto_touched_files[:5]}"
        )
        if _drift_known_bad_status:
            _msg += f" (known_bad guardrail on .py drift: {_drift_known_bad_status})"
        result.setdefault("findings", []).append(_msg)
    if _unattested_outside_scope and isinstance(result, dict):
        result.setdefault("metrics", {})["unattested_outside_scope_count"] = len(
            _unattested_outside_scope
        )
        result.setdefault("findings", []).append(
            "Filesystem drift outside the bounded repository_scope was not acknowledged or "
            "included in gate evidence: " + str(_unattested_outside_scope[:8])
        )

    # ── Data-quality posture (soft, never blocks) (D-003) ──
    try:
        from odibi_anchor._dispatcher._enforcement import data_quality_posture
        _dq_note = data_quality_posture(session_timings)
        if _dq_note and isinstance(result, dict):
            result.setdefault("risks", []).append(_dq_note)
            result.setdefault("suggested_next_actions", []).append(
                'SHOULD: anchor("quality", df, subject="<name>", keys=[...]) — '
                "session executed data transforms without a quality check."
            )
    except Exception as _exc:
        from odibi_anchor._utils._session_state import record_degraded
        record_degraded("gate_data_posture", _exc)

    if isinstance(result, dict):
        _gate_risk = result.get("metrics", {}).get("risk_level")
        # Standalone publication is performed by bootstrap only after SQLite
        # obligation activation succeeds. Checkpoint publishes only its outer result.

    # ── Spec criteria verification (Phase 3: informational, non-blocking) ──
    _active_spec = getattr(session_state, "active_spec", None)
    if isinstance(result, dict) and _active_spec is not None:
        try:
            from odibi_anchor._dispatcher._spec import (
                check_spec_criteria,
                render_spec_criteria,
            )
            _spec_crit = check_spec_criteria(
                _active_spec, session_timings, str(root)
            )
            result["spec_criteria"] = _spec_crit
            _spec_md = render_spec_criteria(_spec_crit)
            if _spec_md and isinstance(result.get("summary"), str):
                result["summary"] = result["summary"] + "\n" + _spec_md
        except Exception as _sc_exc:
            from odibi_anchor._utils._session_state import record_degraded
            record_degraded("gate_spec_criteria", _sc_exc)

    return _attach_assurance_shadow(
        result,
        session_state=session_state,
        session_timings=session_timings,
        deferred=_deferred,
        task_scope=task_scope,
        unattested_paths=_unattested_outside_scope,
    )
