"""Importable bootstrap for odibi_anchor.

Usage when the package is already importable:

    from odibi_anchor.bootstrap import init
    anchor, ROOT, MANIFEST = init()

    # With a managed project beneath odibi_anchor/workspace/projects:
    anchor, ROOT, MANIFEST = init(project="queue-automation")

    # With a backward-compatible external target root:
    anchor, ROOT, MANIFEST = init(root="/path/to/project")

For a fresh source checkout, run the repository-root ``agent_bootstrap.py`` in
the caller's process as documented in the repository workflow reference.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import cast


def _git_revision(path: str | Path) -> str | None:
    """Return a non-secret source revision when the runtime is in a Git checkout."""
    import subprocess

    completed = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--verify", "HEAD"],
        capture_output=True, text=True, timeout=5, check=False,
    )
    revision = completed.stdout.strip()
    return revision if completed.returncode == 0 and len(revision) == 40 else None


_SOURCE_DIGEST_CACHE: dict[str, tuple[int, int, bytes]] = {}


def _source_fingerprint(path: str | Path) -> str | None:
    """Hash the package bytes loaded by this runtime, including untracked modules."""
    del path  # The package location, not the target repository, defines runtime identity.
    package_path = Path(__file__).resolve().parent
    if not package_path.is_dir():
        return None
    digest = hashlib.sha256()
    for candidate in sorted(package_path.rglob("*.py")):
        relative = candidate.relative_to(package_path).as_posix()
        stat = candidate.stat()
        key = str(candidate)
        cached = _SOURCE_DIGEST_CACHE.get(key)
        identity = (stat.st_mtime_ns, stat.st_size)
        if cached is None or cached[:2] != identity:
            cached = (*identity, hashlib.sha256(candidate.read_bytes()).digest())
            _SOURCE_DIGEST_CACHE[key] = cached
        digest.update(relative.encode("utf-8") + b"\0" + cached[2] + b"\0")
    return digest.hexdigest()


_RUNTIME_LOADED_REVISION = _git_revision(Path(__file__).resolve().parent)
_RUNTIME_LOADED_FINGERPRINT = _source_fingerprint(Path(__file__).resolve().parent)


def init(
    root: str | None = None,
    *,
    project: str | None = None,
    route_binding: object | None = None,
    repository_provider: object | None = None,
    rebind_task: bool = False,
    frame_enabled: bool = True,
    output_format: str = "markdown",
) -> tuple:
    """Initialize odibi_anchor session and return (anchor, ROOT, MANIFEST).

    Args:
        root: Backward-compatible explicit target root. If omitted, use the active
            managed project or resolved Odibi Anchor home.
        project: Optional managed project ID beneath ``workspace/projects``.
        route_binding: Optional immutable ``RouteBinding`` resolved by a host runtime.
            When supplied, its project, target, artifacts, and ANCHOR_HOME are authoritative.
        repository_provider: Optional explicit read-only repository identity provider.
            Databricks Git Folder source tasks additionally require repository_scope
            and accept_unknown_git_state=True when this provider is supplied. Use
            mode="implementation", work_type="change", and
            execution_mode="source_change"; stale agents must refresh and rerun the
            current bootstrap rather than fall back to documentation mode.
        rebind_task: Explicitly restore the newest open accepted task for the resolved
            project and target without recapturing or requiring a clean worktree.
        frame_enabled: Whether to enable the context frame for session tracking.
        output_format: Default output format for anchor() actions ("markdown" or "json").

    Returns:
        Tuple of (anchor_dispatcher, ROOT, MANIFEST).
    """
    if type(rebind_task) is not bool:
        raise TypeError("rebind_task must be a bool")
    route_binding_payload = None
    if route_binding is not None:
        serializer = getattr(route_binding, "as_dict", None)
        if not callable(serializer):
            raise TypeError("route_binding must be a RouteBinding")
        route_binding_payload = serializer()
        if not isinstance(route_binding_payload, dict):
            raise TypeError("route_binding must serialize to a dictionary")

    # ── Flush stale module cache ─────────────────────────────────────────────
    for k in list(sys.modules):
        if k.startswith("odibi_anchor"):
            del sys.modules[k]

    # ── Path setup ───────────────────────────────────────────────────────────
    _pkg_dir = os.path.dirname(os.path.abspath(__file__))
    _src_dir = os.path.dirname(_pkg_dir)
    if _src_dir not in sys.path:
        sys.path.insert(0, _src_dir)

    # ── Constants & Root Detection ───────────────────────────────────────────
    from odibi_anchor._dispatcher._boot import (
        FRAMEWORK_ROOT,
        ANCHOR_ROOT,
        _RUNTIME_PATHS,
    )

    ANCHOR_ROOT_LOCAL = str(_RUNTIME_PATHS.anchor_home)
    _resource_root = str(_RUNTIME_PATHS.resource_root)

    # Registered tool wrappers retain the top-level ``tools.*`` import contract.
    # Add only the verified immutable resource root, never cwd or writable home.
    if _resource_root not in sys.path:
        sys.path.insert(0, _resource_root)

    from odibi_anchor._dispatcher._project import (
        RouteBinding,
        project_routing_fingerprint,
        resolve_active_project,
        resolve_route_binding,
        resolve_runtime_roots,
        route_binding_diagnostics,
        route_binding_staleness_reason,
        route_path_identity,
    )

    if route_binding_payload is not None:
        route_binding = RouteBinding(**route_binding_payload)
    if route_binding is not None:
        if project is not None and project != route_binding.project_id:
            raise ValueError("route_binding conflicts with project")
        if root is not None and route_path_identity(root) != route_path_identity(
            route_binding.target_root
        ):
            raise ValueError("route_binding conflicts with root")
        stale_reason = route_binding_staleness_reason(route_binding)
        if stale_reason is not None:
            raise RuntimeError(f"RouteBinding is stale: {stale_reason}")
    elif project is not None:
        route_binding = resolve_route_binding(
            ANCHOR_ROOT_LOCAL,
            project=project,
            target_hint=root,
            runtime_instance_id=f"direct:{os.getpid()}",
        )

    # Route authority must be resolved before learning recovery. Learning debt
    # is owner-scoped and cannot be queried until boot restores an exact task.
    _runtime_roots = resolve_runtime_roots(
        ANCHOR_ROOT_LOCAL,
        explicit_target=root,
        project=project,
        route_binding=route_binding,
    )
    ROOT = _runtime_roots.target_root or _runtime_roots.artifact_root
    _initial_registry = route_binding or resolve_active_project(ANCHOR_ROOT_LOCAL, project)
    _initial_registry_version = project_routing_fingerprint(ANCHOR_ROOT_LOCAL, _initial_registry)
    _routing_stale_reason = [None]
    _initial_source_revision = _RUNTIME_LOADED_REVISION
    _initial_source_fingerprint = _RUNTIME_LOADED_FINGERPRINT

    # ── Session context ──────────────────────────────────────────────────────
    _SESSION_CONTEXT: dict = {}

    # ── Lazy Module loader ───────────────────────────────────────────────────
    class _LazyModule:
        __slots__ = ("_module_path", "_module", "_names")

        def __init__(self, module_path, names):
            object.__setattr__(self, "_module_path", module_path)
            object.__setattr__(self, "_module", None)
            object.__setattr__(self, "_names", names)

        def _load(self):
            mod = object.__getattribute__(self, "_module")
            if mod is None:
                import importlib
                path = object.__getattribute__(self, "_module_path")
                mod = importlib.import_module(path)
                object.__setattr__(self, "_module", mod)
            return mod

        def __getattr__(self, name):
            return getattr(self._load(), name)

    # ── Lazy module declarations ─────────────────────────────────────────────
    _planning = _LazyModule("odibi_anchor.planning", [
        "task_execution_context", "render_task_execution_report",
        "quick_context", "handoff_context", "render_handoff_report",
    ])
    _validation_mod = _LazyModule("odibi_anchor.validation", [
        "quality_gate_context", "render_quality_gate_report",
        "validation_summary_context", "render_validation_report",
        "duplicate_key_context", "render_duplicate_key_report",
    ])
    _tables_mod = _LazyModule("odibi_anchor.tables", [
        "diff_tables_by_key", "render_diff_report", "schema_diff_context",
        "table_contract_summary", "transform_plan_context", "render_transform_plan_report",
        "adapt_profile_to_transform_input", "apply_transform_context", "apply_sql",
        "render_apply_transform_report", "rollback", "unpersist",
        "coercion_check_context", "render_coercion_report",
    ])
    _profiling_mod = _LazyModule("odibi_anchor.profiling", [

        "exploration_context", "render_exploration_report",
        "dogfood_regression_context", "render_dogfood_regression_report",
    ])
    _debugging_mod = _LazyModule("odibi_anchor.debugging", [
        "error_trace_context", "render_error_trace_report",
        "failure_pattern_context", "render_failure_pattern_report",
    ])
    _codebase_mod = _LazyModule("odibi_anchor.codebase", [
        "codebase_map_context", "render_codebase_map_report",
        "change_impact_context", "render_change_impact_report",
        "session_snapshot_context", "render_session_snapshot_report",
        "save_snapshot", "load_snapshot",
        "consistency_check_context", "render_consistency_check_report",
        "convention_preflight_context", "render_convention_preflight_report",
        "test_focus_context", "render_test_focus_report",
        "workflow_gate_context", "render_workflow_gate_report",
        "framework_lookup_context", "render_framework_lookup_report",
        "memory_context", "render_memory_report",
        "append_memory", "confirm_memory", "reject_memory",
        "archive_stale_entries", "export_markdown", "import_from_markdown",
        "semantic_edit_context", "render_semantic_edit_report",
        "preflight_context", "render_preflight_report",
        "import_resolve_context", "render_import_resolve_report",
        "safe_change_context", "render_safe_change_report",
        "learn_context", "render_learn_report",
        "known_bad_change_context", "render_known_bad_change_report",
    ])
    _manifest_mod = _LazyModule("odibi_anchor.codebase._manifest", [
        "load_manifest", "manifest_context",
    ])
    _ast_utils_mod = _LazyModule("odibi_anchor._utils.ast_utils", [
        "ast_cache_invalidate", "ast_cache_clear",
    ])
    _workflows_mod = _LazyModule("odibi_anchor._dispatcher._workflows", [
        "_onboard_workflow", "_reconcile_workflow", "_investigate_workflow",
        "_debug_workflow", "_trace_workflow", "_evolve_workflow",
    ])

    # ── Direct imports ───────────────────────────────────────────────────────
    from odibi_anchor.codebase._memory_db import (
        query_memories as _db_query_memories,
        entry_count as _db_entry_count,
        resolve_project as _db_resolve_project,
        confirm_memory_entry as _db_confirm_entry,
        increment_sessions_seen as _db_increment_sessions_seen,
        promote_by_sessions_seen as _db_promote_by_sessions_seen,
        add_tag_to_entry as _db_add_tag_to_entry,
        count_similar_entries as _db_count_similar_entries,
        insert_audit as _db_insert_audit,
        query_audits as _db_query_audits,
        audit_trend as _db_audit_trend,
        _DEFAULT_DB_PATH,
    )

    from odibi_anchor._utils._tool_registry import (
        Registry as _Registry, ToolSpec as _ToolSpec,
        ToolLoadError as _ToolLoadError, VALID_CATEGORIES as _VALID_TOOL_CATEGORIES,
    )
    _tool_registry = _Registry()
    _tool_registry.discover_tools([str(_RUNTIME_PATHS.tools_dir)])

    from odibi_anchor._utils._context_frame import ContextFrame as _ContextFrame

    ANCHOR_OUTPUT_FORMAT = output_format
    ANCHOR_FRAME_ENABLED = frame_enabled

    _NO_OUTPUT_FORMAT_ACTIONS = frozenset({
        "save", "confirm", "reject", "archive", "export_md", "import_md", "test_run",
        "save_snap", "load_snap", "touched", "unpersist", "rollback", "log", "new_session",
        "skill_loaded", "apply_sql",
    })
    _PLANNING_REQUIRED_ACTIONS = frozenset({
        "safe", "semantic", "gate", "preflight", "checkpoint", "touched",
        "save", "apply_transform", "confirm", "reject", "archive", "import_md",
    })
    from odibi_anchor._dispatcher._effects import (
        ActionContract as _ActionContract,
        BUILTIN_ACTION_NAMES as _BUILTIN_ACTION_NAMES,
        InvocationSemantics as _InvocationSemantics,
        build_static_action_contracts as _build_static_action_contracts,
        resolve_invocation as _resolve_invocation,
    )
    _ACTION_CONTRACTS = _build_static_action_contracts()

    os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    from odibi_anchor._utils._session_state import (
        _SESSION_FILES_CHANGED, _SESSION_FILES_CREATED, _SESSION_TIMINGS,
        _SESSION_LOG, _SESSION_BOOT_MANIFEST, _SESSION_STATE,
        touched as _session_touched, record_timing as _session_record_timing,
        is_empty as _session_is_empty, get_diff as _get_session_diff,
        log_note, get_log,
        build_boot_manifest as _build_boot_manifest,
        check_filesystem_drift as _check_filesystem_drift,
        reconcile_session_file_ledger as _reconcile_session_file_ledger,
        install_write_guard as _install_write_guard,
        reset_session_state,
    )
    _SESSION_STATE.anchor_home = ANCHOR_ROOT_LOCAL
    _SESSION_STATE.active_project = _runtime_roots.active_project
    _SESSION_STATE.project_root = _runtime_roots.project_root
    _SESSION_STATE.artifact_root = _runtime_roots.artifact_root
    _SESSION_STATE.target_root = _runtime_roots.target_root
    _SESSION_STATE.route_schema_version = (
        route_binding.schema_version if route_binding is not None else None
    )
    _SESSION_STATE.route_fingerprint = (
        route_binding.fingerprint() if route_binding is not None else None
    )
    _SESSION_STATE.runtime_instance_id = (
        route_binding.runtime_instance_id if route_binding is not None else None
    )
    _SESSION_STATE.trust_domain = os.environ.get("ANCHOR_TRUST_DOMAIN") or None
    _SESSION_STATE.runtime_loaded_revision = _initial_source_revision
    _SESSION_STATE.runtime_current_revision = _initial_source_revision
    _SESSION_STATE.runtime_loaded_fingerprint = _initial_source_fingerprint
    _SESSION_STATE.runtime_current_fingerprint = _initial_source_fingerprint
    _SESSION_STATE.runtime_source_stale = False
    if repository_provider is not None and not callable(
        getattr(repository_provider, "capture_identity", None)
    ):
        raise TypeError("repository_provider must define capture_identity(target_worktree)")
    _SESSION_STATE.repository_provider = repository_provider
    from odibi_anchor.codebase.review_context import review_context as _review_context_impl
    from odibi_anchor._dispatcher._session import (
        _save_session_state as _save_session_state_impl, _config as _config_impl,
        _session_files as _session_files_impl, _session_diff_action as _session_diff_action_impl,
    )
    from odibi_anchor._dispatcher._compliance import (
        _compliance_audit as _compliance_audit_impl, _skills_registry as _skills_registry_impl,
        _status as _status_impl, _audit_history as _audit_history_impl,
    )
    from odibi_anchor._dispatcher._session_health import (
        capture_session_health as _capture_session_health,
        check_cross_session_drift as _check_cross_session_drift,
        session_delta_context as _session_delta_context_impl,
    )
    from odibi_anchor._dispatcher._memory import (
        _memory_tags as _memory_tags_impl, _memory_stats as _memory_stats_impl,
        _new_session as _new_session_impl,
    )
    from odibi_anchor._dispatcher._session_tools import _test_run as _test_run_impl
    from odibi_anchor._dispatcher._tools import (
        _tools_action as _tools_action_impl, _register_tool_action as _register_tool_action_impl,
    )
    from odibi_anchor._dispatcher._frame import (
        _build_contextual_suggestions as _build_contextual_suggestions_impl,
        _frame_action as _frame_action_impl,
    )
    from odibi_anchor._dispatcher._boot import (
        _rank_boot_entries, _BOOT_MEMORY_LIMIT, _BOOT_TYPE_PRIORITY,
    )
    from odibi_anchor._dispatcher._tool_wrappers import (
        _profile_table_context, _microscope_context,
        _case_file_context, _chain_context,
    )

    # ── Internal helpers (closures over session state) ────────────────────────
    _SESSION_FRAME_holder = [None]  # mutable container for nested function access
    _TASK_STAGE_holder = [None]

    def _save_session_state(state: dict) -> None:
        if route_binding is None:
            _save_session_state_impl(_SESSION_STATE.artifact_root, state, strict=True)
            return
        from odibi_anchor._dispatcher._session import _save_continuity_state

        _save_continuity_state(route_binding, _SESSION_STATE, state)

    def _touched(path: str, *, created: bool = False, **_kwargs) -> dict:
        from odibi_anchor._dispatcher._session import _touched_action
        # Any source-write makes a stored ordinary-evaluation snapshot unusable.
        _SESSION_STATE.repository_snapshot = None
        return _touched_action(path, str(ROOT), created=created, **_kwargs)

    def _checkpoint(*args, **kwargs):
        from odibi_anchor._dispatcher._checkpoint import checkpoint as _ckpt_impl
        if any(name in kwargs for name in (
            "_session_timings", "_session_frame", "_learning_project_id",
        )):
            raise ValueError("checkpoint transaction state is internal-only")
        return _ckpt_impl(
            anchor, _SESSION_FILES_CHANGED, _SESSION_STATE, *args,
            _session_timings=_SESSION_TIMINGS,
            _session_frame=_SESSION_FRAME_holder[0],
            _learning_project_id=_boot_result.project,
            **kwargs,
        )

    def _compliance_audit() -> dict:
        return _compliance_audit_impl()

    def _journal_action(action_name, event_type, action_id, payload):
        """Best-effort forensic evidence that never changes action authority."""
        try:
            from odibi_anchor._forensic_replay import append_event
            from odibi_anchor.codebase._memory_db import _resolve_default_db_path

            append_event(
                _resolve_default_db_path(), _SESSION_STATE.task_window_id,
                event_type, {"action": action_name, "action_id": action_id, **payload},
                event_id=f"{action_id}:{event_type}",
            )
        except Exception as exc:
            from odibi_anchor._utils._session_state import record_degraded
            record_degraded("forensic_journal", exc)

    def _project_dispatch(*project_args, **project_kwargs):
        result = __import__(
            "odibi_anchor._dispatcher._project", fromlist=["project_action"]
        ).project_action(
            ANCHOR_ROOT_LOCAL,
            *project_args,
            current_project=_SESSION_STATE.active_project,
            current_target=_SESSION_STATE.target_root,
            route_binding=route_binding,
            routing_stale=_SESSION_STATE.routing_stale,
            **project_kwargs,
        )
        if isinstance(result, dict) and result.get("reinitialize_required") is True:
            from odibi_anchor._utils._session_state import reset_task_policy_state
            reset_task_policy_state(_SESSION_STATE)
            _SESSION_STATE.routing_stale = True
            result["routing_stale"] = True
            result["task_state_reset"] = True
            result["orientation_required"] = True
            result["fresh_task_required"] = True
            result["suggested_next_actions"] = [
                "This dispatcher is stale. Re-run init(), bind its returned anchor dispatcher, "
                "then orient and start a fresh task."
            ]
        return result

    def _new_session_and_store(*args, **kwargs) -> dict:
        # A named logical session owns a fresh stable identity for all loads and
        # the eventual learn/co-occurrence computation.
        result = _new_session_impl(
            ROOT,
            ANCHOR_ROOT,
            *args,
            artifact_root=_SESSION_STATE.artifact_root,
            project=_SESSION_STATE.active_project,
            **kwargs,
        )
        from odibi_anchor._dispatcher._effects import dispatch_succeeded
        if isinstance(result, dict) and dispatch_succeeded("new_session", result):
            import uuid as _uuid
            from odibi_anchor._utils._session_state import reset_task_policy_state
            orientation_timings = []
            for prerequisite in ("status", "audit_history"):
                matching = [
                    timing for timing in _SESSION_TIMINGS
                    if timing["action"] == prerequisite and timing.get("error") is None
                ]
                if matching:
                    orientation_timings.append(matching[-1])
            reset_task_policy_state(_SESSION_STATE)
            _SESSION_FILES_CHANGED.clear()
            _SESSION_FILES_CREATED.clear()
            _SESSION_TIMINGS.clear()
            _SESSION_TIMINGS.extend(orientation_timings)
            _SESSION_STATE.skills_loaded.clear()
            _SESSION_STATE.session_id = str(_uuid.uuid4())
            _SESSION_STATE.continuity_generation = 0
            _SESSION_STATE.continuity_record_sha256 = None
            _SESSION_STATE.continuity_status = "empty"
            _SESSION_STATE.session_name = result.get("subject")
            nb_path = result.get("metrics", {}).get("notebook_path")
            if nb_path:
                _SESSION_STATE.notebook_path = nb_path
        return result

    def _task_dispatch(*task_args, **task_kwargs):
        import pathlib as _task_pathlib
        injected = _inject_session_context(
            task_kwargs, _SESSION_FRAME_holder[0] if ANCHOR_FRAME_ENABLED else None,
            _SESSION_FILES_CHANGED, _SESSION_STATE.prior_learn_debt,
            task_text=task_args[0] if task_args else "",
            specs_dir=_task_pathlib.Path(_SESSION_STATE.artifact_root) / "specs",
            problems_dir=_task_pathlib.Path(_SESSION_STATE.artifact_root) / "problems",
            session_state=_SESSION_STATE,
        )
        _TASK_STAGE_holder[0] = injected.pop("_task_policy_stage")
        # Availability here means only that this dispatcher recognizes the action.
        # The context selector does not infer dependencies, credentials, or permission.
        _registered_names = (
            [spec.name for spec in _tool_registry.list_tools()]
            if _tool_registry else []
        )
        injected["_available_actions"] = sorted({*_BUILTIN_ACTION_NAMES, *_registered_names})
        result = _planning.task_execution_context(task_args[0] if task_args else "", **injected)
        if isinstance(result, dict):
            from odibi_anchor._dispatcher._project import artifact_contract
            result["artifact_contract"] = artifact_contract()
        return result

    def _task_rebind_action():
        """Restore the newest matching open accepted task from durable authority."""
        from odibi_anchor.codebase._task_authority import rebind_latest_open_task

        rebound = rebind_latest_open_task(_DEFAULT_DB_PATH, session_state=_SESSION_STATE)
        frame = _SESSION_FRAME_holder[0]
        if ANCHOR_FRAME_ENABLED and frame is not None:
            frame.record("task", {
                "kind": "task_execution_context",
                "subject": _SESSION_STATE.task_goal or "",
                "mode": _SESSION_STATE.active_task_mode or "",
                "verification": {
                    "acceptance_criteria": rebound["obligations"]["acceptance_criteria"],
                },
            })
        _SESSION_TIMINGS.append({
            "action": "task",
            "elapsed_ms": 0.0,
            "error": None,
            "passed": True,
            "result": "task_execution_context",
            "task_mode": _SESSION_STATE.active_task_mode,
            "synthetic": "durable_rebind",
        })
        _SESSION_STATE.task_verification_epoch = len(_SESSION_TIMINGS)
        return {"kind": "task_authority_rebind", **rebound}

    def _task_rebind_dispatch(action_args, action_kwargs):
        if action_args or {key for key in action_kwargs if key != "output_format"}:
            raise TypeError("task_rebind accepts no arguments")
        return _task_rebind_action()

    def _task_adoption_action(command="inspect", **options):
        """Request exact owner approval or inspect immutable adoption evidence."""
        from odibi_anchor.codebase._adopted_dirty import (
            AdoptionUnavailable,
            inspect_adoptions,
            record_adoption_refusal,
            request_adoption_approval,
            withdraw_adoption,
        )

        normalized = str(command).strip().lower()
        if normalized == "inspect":
            if options:
                raise TypeError("task_adoption inspect accepts no arguments")
            from odibi_anchor.codebase._task_authority import inspect_task_authority

            authority = inspect_task_authority(_DEFAULT_DB_PATH)
            return {
                "kind": "task_adoption_context", **inspect_adoptions(_DEFAULT_DB_PATH),
                "durable_windows": authority["durable_windows"],
                "rebindings": authority["rebindings"],
                "task_authority_schema_status": authority["schema_status"],
            }
        if normalized == "withdraw":
            allowed = {"adoption_id", "reason"}
            unknown = set(options) - allowed
            if unknown:
                raise TypeError(
                    f"task_adoption withdraw received unsupported arguments: {sorted(unknown)}"
                )
            actor = os.environ.get("ANCHOR_SLACK_USER_ID", "")
            return {
                "kind": "task_adoption_withdrawal",
                **withdraw_adoption(
                    _DEFAULT_DB_PATH, adoption_id=options.get("adoption_id", ""),
                    reason=options.get("reason", ""), actor_ref=actor,
                ),
            }
        if normalized != "request":
            raise ValueError("task_adoption command must be request, withdraw, or inspect")
        allowed = {"prior_task_window_id", "timeout_minutes"}
        unknown = set(options) - allowed
        if unknown:
            raise TypeError(f"task_adoption request received unsupported arguments: {sorted(unknown)}")
        prior = options.get("prior_task_window_id")
        if not isinstance(prior, str) or not prior.strip():
            raise TypeError("prior_task_window_id must be a non-empty string")
        try:
            approval = request_adoption_approval(
                _DEFAULT_DB_PATH, prior_task_window_id=prior.strip(),
                project_id=_SESSION_STATE.active_project,
                target_root=_SESSION_STATE.target_root,
                artifact_root=_SESSION_STATE.artifact_root,
                trust_domain=os.environ.get("ANCHOR_TRUST_DOMAIN", ""),
                expected_owner_id=os.environ.get("ANCHOR_SLACK_USER_ID", ""),
                timeout_minutes=options.get("timeout_minutes", 60),
            )
        except AdoptionUnavailable as exc:
            record_adoption_refusal(
                _DEFAULT_DB_PATH, exc, operation="request",
                prior_task_window_id=prior.strip(),
            )
            raise
        return {"kind": "task_adoption_approval", **approval}

    def _task_adoption_dispatch(action_args, action_kwargs):
        command = action_args[0] if action_args else "inspect"
        if len(action_args) > 1:
            raise TypeError("task_adoption accepts at most one positional command")
        options = {key: value for key, value in action_kwargs.items() if key != "output_format"}
        return _task_adoption_action(command, **options)

    def _learning_dispatch(learning_args, learning_kwargs):
        command = str(learning_args[0]).lower().strip() if learning_args else "list"
        if command == "safe_stop":
            from odibi_anchor._dispatcher._safe_stop import safe_stop_task
            return safe_stop_task(
                status=learning_kwargs.get("status"), reason=learning_kwargs.get("reason"),
                unavailable_evidence=learning_kwargs.get("unavailable_evidence"),
                learning_project_id=_boot_result.project,
                session_state=_SESSION_STATE, session_timings=_SESSION_TIMINGS,
            )

        from odibi_anchor.codebase.structured_learning_context import (
            _structured_learning_dispatch,
            active_learning_obligation,
            ensure_learning_obligation,
            initialize_learning_schema,
            latest_closed_learning_obligation,
        )
        if command not in {"capture", "assess"}:
            return _structured_learning_dispatch(command=command, **learning_kwargs)
        owner_project = _SESSION_STATE.active_project or _boot_result.project
        owner_task = _SESSION_STATE.task_window_id
        if not owner_project or not owner_task:
            raise RuntimeError(
                "Structured learning requires an exact project and task window"
            )
        retry_latest = bool(learning_kwargs.get("retry_latest"))
        try:
            active = active_learning_obligation(
                project_id=owner_project, task_window_id=owner_task,
            )
        except RuntimeError as exc:
            if str(exc) != "learning schema drift":
                raise
            initialize_learning_schema()
            active = active_learning_obligation(
                project_id=owner_project, task_window_id=owner_task,
            )
        if not retry_latest and active is None:
            obligation = ensure_learning_obligation(
                task_window_id=owner_task,
                session_ref="session:" + _SESSION_STATE.session_id.replace("-", ""),
                checkpoint_ref=command + ":" + str(len(_SESSION_TIMINGS) + 1),
                project_id=owner_project,
                project_ref=owner_project,
            )
            _SESSION_STATE.learning_obligation_id = obligation["obligation_id"]
            active = active_learning_obligation(
                project_id=owner_project, task_window_id=owner_task,
            )
        internal = {}
        selected = (
            latest_closed_learning_obligation(
                project_id=owner_project, task_window_id=owner_task,
            )
            if retry_latest else active
        )
        if selected is not None:
            key = "_latest_closed_obligation_id" if retry_latest else "_obligation_id"
            internal[key] = selected["obligation_id"]
        internal.update({
            "_project_id": owner_project,
            "_task_window_id": owner_task,
        })
        return _structured_learning_dispatch(
            command=command, **learning_kwargs, **internal,
        )

    def _orient(*args, **kwargs):
        """AXI pre-computed aggregate (round-trip elimination): run status and
        audit_history in ONE call. Each step is routed through anchor() so its
        enforcement timing is recorded. Task-aware memory retrieval occurs only
        after task acceptance.
        """
        _of = kwargs.get("output_format", "dict")
        _parts: dict = {}
        for _step in ("status", "audit_history"):
            try:
                _parts[_step] = anchor(_step, output_format="dict")
            except Exception as _e:  # orientation must never crash
                _parts[_step] = {"error": f"{type(_e).__name__}: {_e}"}
        from odibi_anchor._dispatcher._capture_standards import (
            capture_standards_contract,
            render_capture_standards_markdown,
        )
        from odibi_anchor._dispatcher._project import artifact_contract
        from odibi_anchor._utils.contract import build_base_context
        _st = _parts.get("status") if isinstance(_parts.get("status"), dict) else {}
        _next = (_st.get("metrics") or {}).get("next_required_action")
        _summary = "Orientation: ran status + audit_history; task memory is deferred to acceptance."
        _artifact_contract = artifact_contract()
        _capture_standards = capture_standards_contract()
        ctx = build_base_context(
            kind="orientation",
            subject="session",
            summary=_summary,
            metrics={"next_required_action": _next},
            findings=[_st.get("summary", "status unavailable")],
            risks=[],
            samples={},
            suggested_next_actions=[
                f"MUST: {_next}" if _next else 'Proceed: anchor("task", goal="...", mode="...").',
            ],
            status=_parts.get("status"),
            memory={
                "status": "deferred",
                "reason": "bounded task-aware retrieval occurs at task acceptance",
            },
            audit_history=_parts.get("audit_history"),
            artifact_contract=_artifact_contract,
            capture_standards=_capture_standards,
        )
        if _of == "markdown":
            _lines = ["# Orientation\n", f"**{_summary}**\n"]
            if _next:
                _lines.append(f"**▶ Next required:** `{_next}`\n")
            _lines.extend([
                f"## Managed artifact contract v{_artifact_contract['version']}",
                "",
                _artifact_contract["root_rule"],
                "",
            ])
            for _artifact in _artifact_contract["artifacts"]:
                _lines.append(
                    f"- `{_artifact['path']}` — {_artifact['use_when']} "
                    f"Do not use for: {_artifact['do_not_use_for']}"
                )
            _lines.append("")
            _lines.append(_artifact_contract["activation"])
            _lines.append("")
            _lines.extend([render_capture_standards_markdown(_capture_standards), ""])
            _lines.append(
                "Structured `status` and `audit_history` are included in the dict form; "
                "bounded memory is returned by the accepted task."
            )
            return "\n".join(_lines)
        return ctx

    def _log_with_attestation(*log_args, **log_kwargs):
        """Write a note and optionally ingest an explicit guidance attestation."""
        attestation = log_kwargs.pop("guidance_attestation", None)
        result = log_note(
            log_args[0] if log_args else "",
            log_args[1] if len(log_args) > 1 else "",
            **log_kwargs,
        )
        if attestation is not None:
            if not isinstance(attestation, dict):
                raise ValueError("guidance_attestation must be an object")
            from odibi_anchor._utils._session_state import record_guidance_attestation
            entry = record_guidance_attestation(_SESSION_STATE, **attestation)
            result["guidance_attestation"] = entry.to_dict()
        return result

    def _test_run(*args, **kwargs):
        return _test_run_impl(ROOT, _debugging_mod.failure_pattern_context, *args, **kwargs)

    def _auto_scope_tests(kwargs):
        from odibi_anchor._dispatcher._session_tools import _auto_scope_tests as _ast_impl
        scoped_files = _SESSION_FILES_CHANGED
        baseline = getattr(_SESSION_STATE, "task_repository_baseline", None)
        if baseline is not None:
            from odibi_anchor._repository_snapshot import capture_task_change_scope
            scoped_files = set(capture_task_change_scope(
                baseline, _SESSION_STATE.task_repository_write_fingerprints,
            ).changed_paths)
        return _ast_impl(kwargs, session_files_changed=scoped_files,
                         test_focus_fn=_codebase_mod.test_focus_context, root=ROOT)

    def _active_task_scope():
        baseline = getattr(_SESSION_STATE, "task_repository_baseline", None)
        if baseline is None:
            return None
        from odibi_anchor._repository_snapshot import capture_task_change_scope
        return capture_task_change_scope(
            baseline, _SESSION_STATE.task_repository_write_fingerprints,
        )

    def _review_with_task_scope(**review_kwargs):
        scope = _active_task_scope()
        current_frame = _SESSION_FRAME_holder[0]
        result = _review_context_impl(
            ROOT,
            frame=current_frame if ANCHOR_FRAME_ENABLED else None,
            session_diff=(
                __import__(
                    "odibi_anchor._repository_snapshot",
                    fromlist=["task_scope_review_diff"],
                ).task_scope_review_diff(scope)
                if scope is not None else _get_session_diff(root=str(ROOT))
            ),
            test_timings=[
                timing for timing in _SESSION_TIMINGS[(_SESSION_STATE.task_verification_epoch or 0):]
                if timing["action"] == "test" and timing.get("error") is None
            ],
            acceptance_criteria=(
                getattr(getattr(current_frame, "plan_context", None), "acceptance_criteria", None)
                if ANCHOR_FRAME_ENABLED and current_frame else None
            ),
            files_changed=(set(scope.changed_paths) if scope is not None else _SESSION_FILES_CHANGED),
            **review_kwargs,
        )
        if (
            isinstance(result, dict)
            and getattr(scope, "evidence_kind", None) == "databricks_git_folder"
        ):
            result.setdefault("metrics", {}).update({
                "host_head_start": scope.provenance["host_head_start"],
                "host_head_current": scope.provenance["host_head_current"],
                "host_identity_stability": scope.provenance["host_identity_stability"],
                "repository_capabilities": dict(scope.provenance["capabilities"]),
            })
            result.setdefault("risks", []).append(
                "Review covers only task-start-to-current bytes in repository_scope; local Git "
                "cleanliness and complete Git diff/PR readiness remain unavailable."
            )
            result.setdefault("suggested_next_actions", []).append(
                "MUST: Inspect the complete Databricks Repos UI diff before manual commit/push."
            )
        if (
            isinstance(result, dict)
            and scope is not None
            and scope.provenance.get("baseline_authority") == "adopted"
        ):
            adoption = dict(scope.provenance.get("adoption") or {})
            from odibi_anchor.codebase._adopted_dirty import adoption_status

            adoption["status"] = adoption_status(
                _DEFAULT_DB_PATH, adoption_id=adoption["adoption_id"],
            )["status"]
            result.setdefault("metrics", {})["baseline_authority"] = "adopted"
            result.setdefault("samples", {})["adoption_provenance"] = adoption
            result.setdefault("findings", []).insert(
                0,
                "Review covers the complete original diff from the prior accepted task's "
                "task-start baseline under adopted authority.",
            )
        return result

    def _inject_session_context(kwargs, frame, files_changed, learn_debt, *, task_text="", specs_dir=None, problems_dir=None, session_state=None):
        from odibi_anchor._dispatcher._task_injection import inject_session_context
        return inject_session_context(kwargs, frame, files_changed, learn_debt,
                                       task_text=task_text, specs_dir=specs_dir, problems_dir=problems_dir,
                                       session_state=session_state)

    def _skill_loaded_action(name: str, **kwargs) -> dict:
        """Load complete skill guidance, then register it in the current process.

        Hardening (H-002): previously accepted any string without checking the
        skill existed or was read, so the task-derived skill-load gate could
        be satisfied by registering a name the agent never opened. This now:
          1. Normalizes 'skills/<name>/SKILL.md' or '<name>' to the bare name.
          2. Validates <name> resolves in Odibi Anchor's central skills directory
             (raises ValueError listing available skills on a bad name).
          3. Returns the complete resolved content before the invocation can be
             treated as a successful load. Preview-only discovery belongs to
             anchor("skills") and cannot satisfy the skill-load gate.
        """
        if not name or not name.strip():
            raise ValueError("skill name must be non-empty: anchor(\"skill_loaded\", \"skill-name\")")
        unsupported = sorted(set(kwargs) - {"include_content", "output_format"})
        if unsupported:
            raise ValueError(
                f"Unsupported skill_loaded argument(s): {', '.join(unsupported)}. "
                "Complete content is mandatory."
            )
        if "include_content" in kwargs and kwargs["include_content"] is not True:
            raise ValueError(
                "skill_loaded always returns complete guidance before registration; "
                "use anchor(\"skills\") for discovery"
            )
        _name = (
            name.strip().replace("\\", "/")
            .removeprefix("skills/")
            .removesuffix("/SKILL.md")
            .strip("/")
        )
        from odibi_anchor._dispatcher._boot import _resolve_skills_dir
        _skills_dir = _resolve_skills_dir()
        from odibi_anchor._dispatcher._guidance import resolve_and_load_guidance
        _target, _content, _paths = resolve_and_load_guidance(_skills_dir, _name)
        _line_count = _content.count("\n") + 1
        _projected_loaded = sorted({*_SESSION_STATE.skills_loaded, _name})
        _result = {
            "registered": _name,
            "skills_loaded": _projected_loaded,
            "char_count": len(_content),
            "line_count": _line_count,
            "path": f"skills/{_name}/SKILL.md",
            "guidance_target": {"skill": _target.skill},
            "resolved_paths": [str(_path.relative_to(_skills_dir)).replace("\\", "/") for _path in _paths],
            "content_sha256": hashlib.sha256(_content.encode("utf-8")).hexdigest(),
            "content": _content,
            "summary": f"Loaded complete skill {_name!r} ({_line_count} lines) and registered it.",
        }
        return _result


    def _sync_instructions() -> dict:
        """Sync exact instruction bytes from the immutable runtime resource."""
        from odibi_anchor._dispatcher._boot import _USER_HOME

        source = _RUNTIME_PATHS.instructions_file
        dest = Path(_USER_HOME) / ".assistant_instructions.md"
        try:
            source_bytes = source.read_bytes()
        except OSError as exc:
            return {"error": f"Immutable instruction resource could not be read: {type(exc).__name__}", "synced": False}
        destination_bytes = None
        if dest.is_file():
            try:
                destination_bytes = dest.read_bytes()
            except OSError as exc:
                return {"error": f"Instruction destination could not be read: {type(exc).__name__}", "synced": False}
        changed = destination_bytes != source_bytes
        if changed:
            try:
                dest.write_bytes(source_bytes)
            except OSError as exc:
                return {"error": f"Instruction destination could not be written: {type(exc).__name__}", "synced": False}
        digest = hashlib.sha256(source_bytes).hexdigest()
        return {
            "synced": changed,
            "changed": changed,
            "source": str(source),
            "dest": str(dest),
            "byte_count": len(source_bytes),
            "sha256": digest,
            "message": "Instructions synced successfully" if changed else "Already in sync",
        }

    def _build_contextual_suggestions(frame, result):
        return _build_contextual_suggestions_impl(frame, result)

    def _help_action_impl(args, kwargs):
        from odibi_anchor._dispatcher._dispatch_table import build_help_text
        target = args[0] if args else None
        _action_funcs = {
            "memory": _codebase_mod.memory_context, "map": _codebase_mod.codebase_map_context,
            "concurrency": __import__(
                "odibi_anchor._dispatcher._concurrency", fromlist=["concurrency_action"]
            ).concurrency_action,
            "impact": _codebase_mod.change_impact_context, "consistency": _codebase_mod.consistency_check_context,
            "convention": _codebase_mod.convention_preflight_context, "safe": _codebase_mod.safe_change_context,
            "semantic": _codebase_mod.semantic_edit_context, "import_resolve": _codebase_mod.import_resolve_context,
            "known_bad": _codebase_mod.known_bad_change_context,
            "task": _planning.task_execution_context,
            "task_adoption": _task_adoption_action,
            "task_rebind": _task_rebind_action,
            "work_item": __import__(
                "odibi_anchor._dispatcher._work_item", fromlist=["work_item_action"]
            ).work_item_action,
            "incident_snapshot": __import__("odibi_anchor.operational", fromlist=["incident_snapshot"]).incident_snapshot,
            "environment_diff": __import__("odibi_anchor.operational", fromlist=["compare_environments"]).compare_environments,
            "spark_diagnose": __import__("odibi_anchor.operational", fromlist=["spark_diagnose"]).spark_diagnose,
            "uc_context": __import__("odibi_anchor.operational", fromlist=["uc_context"]).uc_context,
            "delta_changes": __import__("odibi_anchor.operational", fromlist=["delta_changes"]).delta_changes,
            "run_diff": __import__("odibi_anchor.operational", fromlist=["run_diff"]).run_diff,
            "observe_table": __import__("odibi_anchor.operational", fromlist=["observe_table"]).observe_table,
            "table_trend": __import__("odibi_anchor.operational", fromlist=["table_trend"]).table_trend,
            "gate": _codebase_mod.workflow_gate_context,
            "preflight": _codebase_mod.preflight_context, "test": _codebase_mod.test_focus_context,
            "profile_table": _profile_table_context, "microscope": _microscope_context,
            "case_file": _case_file_context,
            "quality": _validation_mod.quality_gate_context, "validate": _validation_mod.validation_summary_context,
            "duplicate": _validation_mod.duplicate_key_context, "diff": _tables_mod.diff_tables_by_key,
            "schema_diff": _tables_mod.schema_diff_context, "contract": _tables_mod.table_contract_summary,
            "transform": _tables_mod.transform_plan_context, "apply_transform": _tables_mod.apply_transform_context,
            "known_error": _debugging_mod.failure_pattern_context, "trace": _debugging_mod.error_trace_context,
            "lookup": _codebase_mod.framework_lookup_context, "learn": _codebase_mod.learn_context,
            "save": _codebase_mod.append_memory, "confirm": _codebase_mod.confirm_memory,
            "reject": _codebase_mod.reject_memory, "snapshot": _codebase_mod.session_snapshot_context,
            "dogfood": _profiling_mod.dogfood_regression_context,
            "reconcile": _workflows_mod._reconcile_workflow,
            "investigate": _workflows_mod._investigate_workflow,
            "debug": _workflows_mod._debug_workflow,
            "trace_row": _workflows_mod._trace_workflow,
            "evolve": _workflows_mod._evolve_workflow,
            "chain": _chain_context,
        }
        # Registered tools (pre_join, pre_merge, suggest_rules, …) aren't in the
        # hardcoded map above, so without this their help showed only a usage
        # string + example — no Parameters section. Merge their callables in so
        # anchor("help", "<tool>") introspects the real signature like built-ins do.
        if _tool_registry:
            for _spec in _tool_registry.list_tools():
                if _spec.name not in _action_funcs:
                    try:
                        _action_funcs[_spec.name] = _tool_registry.resolve_callable(_spec)
                    except Exception:
                        pass
        return build_help_text(target, _action_funcs)

    def _record_timing(
        action, result, err, pre_dispatch_test_target, pre_dispatch_test_mark=None, elapsed_ms=0,
        invocation_resolution=None, pre_task_decision=None,
    ):
        from odibi_anchor._dispatcher._effects import dispatch_succeeded
        passed = dispatch_succeeded(action, result, err)
        if (action == "gate" and passed and
                _SESSION_STATE.checkpoint_in_progress is None):
            from odibi_anchor.codebase.structured_learning_context import (
                ensure_learning_obligation,
                initialize_learning_schema,
            )
            owner = {
                "task_window_id": _SESSION_STATE.task_window_id,
                "project_id": _SESSION_STATE.active_project or _boot_result.project,
                "project_ref": _SESSION_STATE.active_project or _boot_result.project,
            }
            try:
                obligation = ensure_learning_obligation(
                    session_ref="session:" + _SESSION_STATE.session_id.replace("-", ""),
                    checkpoint_ref="gate:" + str(len(_SESSION_TIMINGS) + 1),
                    **owner,
                )
            except RuntimeError as exc:
                if str(exc) != "learning schema drift":
                    raise
                initialize_learning_schema()
                obligation = ensure_learning_obligation(
                    session_ref="session:" + _SESSION_STATE.session_id.replace("-", ""),
                    checkpoint_ref="gate:" + str(len(_SESSION_TIMINGS) + 1),
                    **owner,
                )
            _SESSION_STATE.learning_obligation_id = obligation["obligation_id"]
            if _SESSION_STATE.checkpoint_in_progress is not None:
                _SESSION_STATE.checkpoint_in_progress["obligation_id"] = obligation["obligation_id"]
            _SESSION_STATE.files_at_last_checkpoint = len(_SESSION_FILES_CHANGED)
            if (_SESSION_STATE.notebook_path and
                    _SESSION_STATE.checkpoint_in_progress is None):
                try:
                    from odibi_anchor._dispatcher._memory import append_gate_to_notebook
                    append_gate_to_notebook(
                        _SESSION_STATE.notebook_path, result, _SESSION_FILES_CHANGED,
                        feature_num=sum(1 for item in _SESSION_TIMINGS
                                        if item["action"] == "gate" and item.get("passed")) + 1,
                    )
                except Exception as exc:
                    from odibi_anchor._utils._session_state import record_degraded
                    record_degraded("gate_notebook_append", exc)
            frame = _SESSION_FRAME_holder[0]
            if ANCHOR_FRAME_ENABLED and frame is not None and isinstance(result, dict):
                frame.record(action, result)
                ctx_suggestions = _build_contextual_suggestions(frame, result)
                if ctx_suggestions:
                    result.setdefault("suggested_next_actions", []).extend(ctx_suggestions)
        if (action == "checkpoint" and isinstance(result, dict) and
                result.get("metrics", {}).get("failed_at") == "pr_draft"):
            return
        entry = {
            "action": action, "elapsed_ms": round(elapsed_ms, 1),
            "error": type(err).__name__ if err else None, "passed": passed,
            "pre_task_access": invocation_resolution.pre_task_access,
            "pre_task_attempt": pre_task_decision.attempt,
        }
        if action == "test" and pre_dispatch_test_target:
            entry["test_target"] = (
                [str(target) for target in pre_dispatch_test_target]
                if isinstance(pre_dispatch_test_target, (list, tuple))
                else str(pre_dispatch_test_target)
            )
        if action == "test" and pre_dispatch_test_mark:
            entry["test_mark"] = str(pre_dispatch_test_mark)
        if action == "checkpoint" and passed:
            entry["includes_learn"] = bool(
                isinstance(result, dict) and result.get("metrics", {}).get("closure_route") == "legacy"
            )
            entry["includes_closure"] = True
        if action == "learning":
            entry["learning_command"] = "pending"
        _SESSION_TIMINGS.append(entry)
        if (action in {"preflight", "test", "gate", "learn", "learning"} and
                _SESSION_STATE.checkpoint_in_progress is not None):
            # Checkpoint-owned entries are provisional input to nested enforcement.
            # The checkpoint truncates them and publishes one final transition.
            return
        if action == "task":
            return  # Task persistence is deferred until post-dispatch acceptance.
        if action in {"gate", "learn", "checkpoint"} and not passed:
            return  # Preserve the prior persisted learn-debt state on failure.
        if action in ("status", "task", "touched", "safe", "semantic",
                       "gate", "checkpoint", "learn"):
            _stage_map = {
                "status": "oriented", "task": "planned",
                "touched": "editing", "safe": "editing", "semantic": "editing",
                "gate": "gated", "checkpoint": "gated", "learn": "learned",
            }
            awaiting = (action == "gate" and passed)
            if action == "learn":
                awaiting = False
            _save_session_state({
                "stage": _stage_map.get(action, "unknown"),
                "files_changed": sorted(_SESSION_FILES_CHANGED),
                "awaiting_learn": awaiting,
                "active_problem": _SESSION_STATE.active_problem,
                "task_window_id": _SESSION_STATE.task_window_id,
                "learning_obligation_id": _SESSION_STATE.learning_obligation_id,
                "latest_closed_obligation_id": _SESSION_STATE.latest_closed_obligation_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })

    def _timed_dispatch(
        action, dispatch, pre_dispatch_test_target, pre_dispatch_test_mark=None,
        invocation_resolution=None, pre_task_decision=None,
    ):
        t0 = time.perf_counter()
        err = None
        result = None
        journal_action_id = None
        if (
            action != "task"
            and _SESSION_STATE.active_task_profile is not None
            and invocation_resolution.pre_task_access == "task_required"
        ):
            journal_action_id = (
                f"{_SESSION_STATE.session_id}:{len(_SESSION_TIMINGS) + 1}:{action}"
            )
            _journal_action(
                action, "start", journal_action_id,
                {"effects": list(invocation_resolution.effects), "phase": "handler"},
            )
        provisional_gate_files = (
            set(_SESSION_FILES_CHANGED), set(_SESSION_FILES_CREATED)
        ) if action == "gate" and _SESSION_STATE.checkpoint_in_progress is None else None
        try:
            result = dispatch[action]()
            frame = _SESSION_FRAME_holder[0]
            nested_checkpoint_step = (
                action in {"preflight", "test", "gate", "learn", "learning"}
                and _SESSION_STATE.checkpoint_in_progress is not None
            )
            rejected_pr_transition = (
                action == "checkpoint" and result.get("metrics", {}).get("failed_at") == "pr_draft"
            ) if isinstance(result, dict) else False
            if (ANCHOR_FRAME_ENABLED and frame is not None and isinstance(result, dict)
                    and action not in {"task", "gate"}
                    and not nested_checkpoint_step and not rejected_pr_transition):
                frame.record(action, result)
                if action == "map":
                    frame.code_context.codebase_map = result
                ctx_suggestions = _build_contextual_suggestions(frame, result)
                if ctx_suggestions:
                    result.setdefault("suggested_next_actions", []).extend(ctx_suggestions)
        except Exception as exc:
            err = exc
        finally:
            elapsed = (time.perf_counter() - t0) * 1000
            try:
                _record_timing(
                    action, result, err, pre_dispatch_test_target, pre_dispatch_test_mark, elapsed,
                    invocation_resolution, pre_task_decision,
                )
            except Exception:
                if provisional_gate_files is not None:
                    previous_changed, previous_created = provisional_gate_files
                    _SESSION_FILES_CHANGED.clear()
                    _SESSION_FILES_CHANGED.update(previous_changed)
                    _SESSION_FILES_CREATED.clear()
                    _SESSION_FILES_CREATED.update(previous_created)
                raise
            finally:
                if journal_action_id is not None:
                    _journal_action(
                        action, "failed" if err else "completed", journal_action_id,
                        {
                            "elapsed_ms": round(elapsed, 1),
                            "error_type": type(err).__name__ if err else None,
                            "phase": "handler",
                        },
                    )
        if err is not None:
            raise err
        return result, err

    # ── Data-contract persistence helpers (D-002) ────────────────────────────
    def _contract_with_save(root, args, kwargs):
        """anchor("contract", df, subject="x", save=True) persists the contract."""
        _save = kwargs.pop("save", False)
        _subject = kwargs.get("subject", "dataframe")
        _ctx = _tables_mod.table_contract_summary(*args, **kwargs)
        if _save and isinstance(_ctx, dict):
            try:
                from odibi_anchor._utils._contracts import save_contract
                _path = save_contract(str(root), _subject, _ctx)
                # findings may be dicts ({severity,message}); notices go to
                # suggested_next_actions (always strings) + a metrics flag.
                _ctx.setdefault("metrics", {})["contract_saved_path"] = _path
                _ctx.setdefault("suggested_next_actions", []).append(
                    f"NOTE: Contract saved to {_path} — anchor('quality', df, "
                    f"subject='{_subject}') will auto-validate against it."
                )
            except Exception as _exc:  # never break the build on a save failure
                from odibi_anchor._utils._session_state import record_degraded
                record_degraded("contract_save", _exc)
        return _ctx

    def _quality_with_contract(root, args, kwargs):
        """anchor("quality") auto-fills keys/target_schema from a saved contract."""
        _subject = kwargs.get("subject")
        if _subject and kwargs.get("keys") is None:
            try:
                from odibi_anchor._utils._contracts import (
                    load_contract, contract_keys, contract_schema_map,
                )
                _c = load_contract(str(root), _subject)
                if _c:
                    _k = contract_keys(_c)
                    if _k:
                        kwargs["keys"] = _k
                    if kwargs.get("target_schema") is None:
                        _sm = contract_schema_map(_c)
                        if _sm:
                            kwargs["target_schema"] = _sm
                    _ctx = _validation_mod.quality_gate_context(*args, **kwargs)
                    if isinstance(_ctx, dict):
                        _ctx.setdefault("metrics", {})["contract_autoloaded"] = True
                        _ctx.setdefault("suggested_next_actions", []).append(
                            f"NOTE: Auto-validated against saved contract for "
                            f"'{_subject}' (keys={_k})."
                        )
                    return _ctx
            except Exception as _exc:
                from odibi_anchor._utils._session_state import record_degraded
                record_degraded("quality_contract_autoload", _exc)
        return _validation_mod.quality_gate_context(*args, **kwargs)

    def _save_with_spec(root, args, kwargs):
        """anchor("save") auto-tags manual memories with the active spec (S-4)."""
        from odibi_anchor._dispatcher._auto_confirm import inject_spec_tag
        inject_spec_tag(kwargs, _SESSION_STATE.active_spec_name)
        if _SESSION_STATE.active_problem:
            tags = list(kwargs.get("tags") or [])
            problem_tag = f"problem:{_SESSION_STATE.active_problem}"
            if problem_tag not in tags:
                tags.append(problem_tag)
            kwargs["tags"] = tags
        if _SESSION_STATE.active_project:
            kwargs.setdefault("project", _SESSION_STATE.active_project)
        return _codebase_mod.append_memory(root, *args, **kwargs)

    def _snapshot_with_problem(*args, **kwargs):
        """Attach a compact active-problem projection without duplicating its record."""
        if (
            kwargs.get("mode") == "handoff"
            and route_binding is not None
            and _SESSION_STATE.task_window_id is not None
        ):
            from odibi_anchor._dispatcher._canonical_handoff import canonical_handoff

            canonical_kwargs = dict(kwargs)
            canonical_kwargs.pop("mode", None)
            result = canonical_handoff(
                _SESSION_STATE, route_binding, *args, **canonical_kwargs,
            )
        else:
            result = _codebase_mod.session_snapshot_context(ROOT, *args, **kwargs)
        if not _SESSION_STATE.active_problem:
            return result
        from odibi_anchor._dispatcher._problem import problem_action
        projection = problem_action(
            _SESSION_STATE.artifact_root,
            "resume",
            _SESSION_STATE.active_problem,
            output_format="dict",
        )
        if isinstance(result, dict):
            result["active_problem"] = projection
            return result
        return (
            f"{result}\n\n## Active Problem Record\n"
            f"`{projection['problem_id']}` — stage {projection['stage']}/7 — "
            f"next: {projection['next_action']}\n"
            f"Canonical artifact: `{projection['artifact_path']}`"
        )

    # ── The anchor() dispatcher ──────────────────────────────────────────────────
    def anchor(_action, *args, **kwargs):
        """Convenience dispatcher for ALL odibi_anchor operations.

        Run anchor("help") for full API reference, or anchor("help", "action_name") for details.
        """
        action = _action
        frame = _SESSION_FRAME_holder[0]
        current_source_fingerprint = _source_fingerprint(_RUNTIME_PATHS.resource_root)
        current_source_revision = (
            _git_revision(_RUNTIME_PATHS.resource_root)
            if current_source_fingerprint != _initial_source_fingerprint
            else _initial_source_revision
        )
        _SESSION_STATE.runtime_current_revision = current_source_revision
        _SESSION_STATE.runtime_current_fingerprint = current_source_fingerprint
        _SESSION_STATE.runtime_source_stale = bool(
            _initial_source_fingerprint and current_source_fingerprint
            and current_source_fingerprint != _initial_source_fingerprint
        )
        current_registry_version = __import__(
            "odibi_anchor._dispatcher._project", fromlist=["project_routing_fingerprint"]
        ).project_routing_fingerprint(ANCHOR_ROOT_LOCAL, _initial_registry)
        if current_registry_version != _initial_registry_version:
            from odibi_anchor._utils._session_state import reset_task_policy_state
            reset_task_policy_state(_SESSION_STATE)
            _SESSION_STATE.routing_stale = True
            if route_binding is not None:
                _routing_stale_reason[0] = route_binding_staleness_reason(route_binding)
        _concurrency_command = str(
            args[0] if args else kwargs.get("command", "inspect")
        ).strip().lower()
        _concurrency_diagnostic = (
            action == "concurrency" and _concurrency_command in {"inspect", "dry_run"}
        )
        routing_stale_safe = (
            action == "help" or
            _concurrency_diagnostic or
            (action == "project" and (not args or str(args[0]).lower().strip() in {"", "list", "status"}))
        )
        source_stale_safe = action in {"help", "status"} or _concurrency_diagnostic or (
            action == "project" and (not args or str(args[0]).lower().strip() in {"", "list", "status"})
        )
        if _SESSION_STATE.routing_stale and not routing_stale_safe:
            detail = (
                f" {_routing_stale_reason[0]}." if _routing_stale_reason[0] else ""
            )
            raise RuntimeError(
                "BLOCKED: project routing changed and this dispatcher is stale."
                + detail + " "
                "Re-run init() and bind the newly returned anchor dispatcher before continuing."
            )
        if _SESSION_STATE.runtime_source_stale and not source_stale_safe:
            raise RuntimeError(
                "BLOCKED: the running dispatcher was loaded from a different source revision. "
                "Restart or rebootstrap, bind the new anchor dispatcher, then rerun the action."
            )
        if action == "checkpoint":
            for name in ("final", "generate_pr_draft"):
                if name in kwargs and kwargs[name] is not None and type(kwargs[name]) is not bool:
                    raise TypeError(f"{name} must be a bool" + (" or None" if name == "generate_pr_draft" else ""))
        if (action == "task" and "generate_pr_draft" in kwargs and
                kwargs["generate_pr_draft"] is not None and
                type(kwargs["generate_pr_draft"]) is not bool):
            raise TypeError("generate_pr_draft must be a bool or None")
        if action == "learning" and any(
            name in kwargs for name in ("task_window_id", "obligation_id", "_obligation_id")
        ):
            raise ValueError("learning obligation and task-window identifiers are internal-only")
        if (action == "learning" and kwargs.get("retry_latest") and
                _SESSION_STATE.checkpoint_in_progress is not None):
            raise ValueError("retry_latest is unavailable inside checkpoint")

        # Determine the real authority before any pre-dispatch authorization.
        # Registered metadata wins even if stale help/static metadata uses its name.
        _registered_spec = _tool_registry.get_tool(action) if _tool_registry else None
        if action not in _BUILTIN_ACTION_NAMES and _registered_spec is None:
            from difflib import get_close_matches as _gcm
            _all_actions = list(_BUILTIN_ACTION_NAMES) + (
                [s.name for s in _tool_registry.list_tools()] if _tool_registry else []
            )
            _suggestions = _gcm(action, _all_actions, n=3, cutoff=0.6)
            _hint = f" Did you mean: {', '.join(_suggestions)}?" if _suggestions else ""
            raise ValueError(
                f"Unknown anchor() action: '{action}'.{_hint}\n"
                f"Available: {', '.join(sorted(_all_actions))}"
            )
        if action == "skill_loaded" and len(args) != 1:
            raise ValueError(
                "skill_loaded requires exactly one positional skill name; "
                "complete content is mandatory"
            )

        # ── TOON output (AXI token-efficient output) ──
        # Intercept at the dispatcher so every builder benefits with no per-builder
        # change: run the handler in "dict" mode, then selective-TOON encode the
        # result dict after post-dispatch. Builders never see "toon".
        _want_toon = False
        _deferred_markdown = False
        if action not in _NO_OUTPUT_FORMAT_ACTIONS:
            kwargs.setdefault("output_format", ANCHOR_OUTPUT_FORMAT)
            if kwargs.get("output_format") == "toon":
                _want_toon = True
                kwargs["output_format"] = "dict"
            elif kwargs.get("output_format") == "markdown" and (
                action in {
                    "orient", "task", "problem", "spec", "work_item", "project", "preflight", "test", "gate", "checkpoint", "learn", "learning",
                    "incident_snapshot", "environment_diff", "spark_diagnose", "uc_context",
                    "delta_changes", "run_diff", "observe_table", "table_trend",
                }
                or (action == "snapshot" and kwargs.get("mode") == "handoff")
            ):
                _deferred_markdown = True
                kwargs["output_format"] = "dict"
        else:
            kwargs.pop("output_format", None)

        # ── Pre-dispatch enforcement ──
        from odibi_anchor._dispatcher._pre_dispatch import run_pre_dispatch_enforcement
        if _registered_spec is not None:
            _allowed = frozenset(_registered_spec.allowed_effects)
            _registered_effect = next(iter(_allowed)) if len(_allowed) == 1 else None
            _action_contract = _ActionContract(
                allowed_effects=_allowed,
                allowed_pre_task_access=frozenset({_registered_spec.pre_task_access}),
                resolve_invocation=(
                    lambda _args, _kwargs, effect=_registered_effect, access=_registered_spec.pre_task_access:
                    _InvocationSemantics(effect, access)
                )
                if _registered_effect is not None else None,
            )
        else:
            _action_contract = _ACTION_CONTRACTS[action]
        _invocation_resolution = _resolve_invocation(_action_contract, args, kwargs)
        _pre_task_decision = run_pre_dispatch_enforcement(
            action, args, kwargs,
            session_state=_SESSION_STATE,
            session_timings=_SESSION_TIMINGS,
            session_files_changed=_SESSION_FILES_CHANGED,
            session_boot_manifest=_SESSION_BOOT_MANIFEST,
            planning_required_actions=_PLANNING_REQUIRED_ACTIONS,
            root=ROOT,
            invocation_resolution=_invocation_resolution,
            route_binding=route_binding,
        )

        if action == "test":
            if len(args) > 1:
                raise ValueError("anchor('test') accepts at most one positional target")
            if args:
                if "target" in kwargs:
                    raise ValueError("Pass the test target either positionally or with target=, not both")
                kwargs = {**kwargs, "target": args[0]}
                args = ()
            if "target" in kwargs and not kwargs["target"]:
                raise ValueError("target must be a non-empty path or collection of paths")
            kwargs = _auto_scope_tests(kwargs)

        if action == "learning" and any(str(key).startswith("_") for key in kwargs):
            raise ValueError("internal learning payload keys are not caller-accessible")

        # ── Auto-confirm / gate wrappers ──
        from odibi_anchor._dispatcher._auto_confirm import (
            error_with_auto_confirm as _error_ac,
            known_bad_with_auto_confirm as _known_bad_ac,
            learn_with_auto_confirm as _learn_ac,
        )
        from odibi_anchor._dispatcher._gate_wrappers import (
            preflight_with_baseline as _preflight_wb,
            check_test_coverage as _check_tc,
            gate_with_auto_confirm as _gate_ac,
        )

        import pathlib as _pathlib
        _operational_kwargs = {key: value for key, value in kwargs.items() if key != "output_format"}

        _dispatch = {
            # ── Codebase & Memory ──
            "memory":       lambda: __import__(
                "odibi_anchor._dispatcher._memory_actions", fromlist=["memory_action"]
            ).memory_action(
                ROOT, args, kwargs,
                session_state=_SESSION_STATE, query_fn=_codebase_mod.memory_context,
                render_fn=_codebase_mod.render_memory_report,
            ),
            "context":      lambda: __import__(
                "odibi_anchor._dispatcher._agent_context", fromlist=["build_agent_context"]
            ).build_agent_context(
                _SESSION_STATE,
                protocol=__import__(
                    "odibi_anchor._dispatcher._operating_protocol",
                    fromlist=["build_operating_protocol"],
                ).build_operating_protocol(
                    _SESSION_STATE, action="context", session_timings=_SESSION_TIMINGS,
                ),
                route_binding=route_binding,
                **kwargs,
            ),
            "prepare":      lambda: __import__(
                "odibi_anchor._dispatcher._action_preparation", fromlist=["prepare_action"]
            ).prepare_action(
                _SESSION_STATE, route_binding=route_binding, **kwargs,
            ),
            "map":          lambda: _codebase_mod.codebase_map_context(ROOT, *args, **kwargs),
            "impact":       lambda: _codebase_mod.change_impact_context(ROOT, *args, **kwargs),
            "consistency":  lambda: _codebase_mod.consistency_check_context(ROOT, *args, **kwargs),
            "convention":   lambda: _codebase_mod.convention_preflight_context(ROOT, *args, **kwargs),
            "safe":         lambda: _codebase_mod.safe_change_context(ROOT, *args, **{**kwargs, "frame": frame if ANCHOR_FRAME_ENABLED else None}),
            "semantic":     lambda: _codebase_mod.semantic_edit_context(ROOT, *args, **kwargs),
            "import_resolve": lambda: _codebase_mod.import_resolve_context(ROOT, *args, **kwargs),
            "known_bad":    lambda: _known_bad_ac(ROOT, args, kwargs, known_bad_fn=_codebase_mod.known_bad_change_context, add_tag_fn=_db_add_tag_to_entry, render_fn=_codebase_mod.render_known_bad_change_report),
            "learning":     lambda: _learning_dispatch(args, kwargs),

            # ── Planning & Workflow ──
            "task":         lambda: _task_dispatch(*args, **kwargs),
            "task_adoption": lambda: _task_adoption_dispatch(args, kwargs),
            "task_rebind":  lambda: _task_rebind_dispatch(args, kwargs),
            "gate":         lambda: _gate_ac(ROOT, args, kwargs, session_timings=_SESSION_TIMINGS, session_files_changed=_SESSION_FILES_CHANGED, session_files_created=_SESSION_FILES_CREATED, session_frame=frame, frame_enabled=ANCHOR_FRAME_ENABLED, session_state=_SESSION_STATE, workflow_gate_fn=_codebase_mod.workflow_gate_context, check_drift_fn=_check_filesystem_drift, check_test_coverage_fn=_check_tc, reconcile_ledger_fn=_reconcile_session_file_ledger, memory_db=_DEFAULT_DB_PATH),
            "preflight":    lambda: _preflight_wb(ROOT, kwargs, session_frame=frame, frame_enabled=ANCHOR_FRAME_ENABLED, session_files_changed=_SESSION_FILES_CHANGED, preflight_fn=_codebase_mod.preflight_context, session_state=_SESSION_STATE),
            "test":         lambda: _test_run(*args, **kwargs),
            "checkpoint":   lambda: _checkpoint(*args, **kwargs),
            "spec":         lambda: __import__("odibi_anchor._dispatcher._spec", fromlist=["spec_action"]).spec_action(ROOT, *args, specs_dir=_pathlib.Path(_SESSION_STATE.artifact_root) / "specs", **kwargs),
            "problem":      lambda: __import__("odibi_anchor._dispatcher._problem", fromlist=["problem_action"]).problem_action(_SESSION_STATE.artifact_root, *args, project_id=_SESSION_STATE.active_project, **kwargs),
            "work_item":    lambda: __import__("odibi_anchor._dispatcher._work_item", fromlist=["work_item_action"]).work_item_action(_SESSION_STATE.artifact_root, *args, **kwargs),

            # ── Operational Evidence ──
            "incident_snapshot": lambda: __import__("odibi_anchor.operational", fromlist=["incident_snapshot"]).incident_snapshot(
                *args, artifact_root=_SESSION_STATE.artifact_root,
                attach=lambda problem_id, evidence: __import__(
                    "odibi_anchor._dispatcher._problem", fromlist=["problem_action"]
                ).problem_action(
                    _SESSION_STATE.artifact_root, "update", problem_id,
                    evidence=evidence, output_format="dict",
                    project_id=_SESSION_STATE.active_project,
                ),
                **_operational_kwargs,
            ),
            "environment_diff": lambda: __import__("odibi_anchor.operational", fromlist=["compare_environments"]).compare_environments(*args, **_operational_kwargs),
            "spark_diagnose": lambda: __import__("odibi_anchor.operational", fromlist=["spark_diagnose"]).spark_diagnose(*args, **_operational_kwargs).to_dict(),
            "uc_context": lambda: __import__("odibi_anchor.operational", fromlist=["uc_context"]).uc_context(*args, **_operational_kwargs).to_dict(),
            "delta_changes": lambda: __import__("odibi_anchor.operational", fromlist=["delta_changes"]).delta_changes(*args, **_operational_kwargs).to_dict(),
            "run_diff": lambda: __import__("odibi_anchor.operational", fromlist=["run_diff"]).run_diff(*args, **_operational_kwargs).to_dict(),
            "observe_table": lambda: __import__("odibi_anchor.operational", fromlist=["observe_table"]).observe_table(
                *args, artifact_root=_SESSION_STATE.artifact_root if _operational_kwargs.get("persist", False) else None,
                **_operational_kwargs,
            ),
            "table_trend": lambda: __import__("odibi_anchor.operational", fromlist=["table_trend"]).table_trend(
                *args, artifact_root=_SESSION_STATE.artifact_root, **_operational_kwargs,
            ),

            # ── Composed Workflows ──
            "reconcile":   lambda: _workflows_mod._reconcile_workflow(*args, **kwargs),
            "investigate": lambda: _workflows_mod._investigate_workflow(*args, **kwargs),
            "debug":       lambda: _workflows_mod._debug_workflow(*args, **kwargs),
            "trace_row":   lambda: _workflows_mod._trace_workflow(*args, **kwargs),
            "evolve":      lambda: _workflows_mod._evolve_workflow(*args, **kwargs),
            "chain":       lambda: _chain_context(*args, **kwargs),

            # ── Data & Profiling ──
            "profile_table": lambda: _profile_table_context(*args, **kwargs),
            "microscope":   lambda: _microscope_context(*args, **kwargs),
            "case_file":    lambda: _case_file_context(*args, **kwargs),
            "quality":      lambda: _quality_with_contract(ROOT, args, kwargs),
            "validate":     lambda: _validation_mod.validation_summary_context(*args, **dict(kwargs, frame=frame if ANCHOR_FRAME_ENABLED else None)),
            "duplicate":    lambda: _validation_mod.duplicate_key_context(*args, **kwargs),
            "diff":         lambda: _tables_mod.diff_tables_by_key(*args, **kwargs),
            "schema_diff":  lambda: _tables_mod.schema_diff_context(*args, **kwargs),
            "contract":     lambda: _contract_with_save(ROOT, args, kwargs),
            "transform":    lambda: _tables_mod.transform_plan_context(*args, **kwargs),
            "apply_transform": lambda: _tables_mod.apply_transform_context(*args, **kwargs),
            "apply_sql":    lambda: _tables_mod.apply_sql(args[0] if args else kwargs.pop("code_sql", ""), **kwargs),
            "rollback": lambda: _tables_mod.rollback(args[0] if args else {}, to=kwargs.get("to", args[1] if len(args) > 1 else "start")),
            "unpersist": lambda: _tables_mod.unpersist(args[0] if args else {}, **kwargs),
            "coerce_check": lambda: _tables_mod.coercion_check_context(*args, **kwargs),

            # ── Debugging & Learning ──
            "known_error":  lambda: _error_ac(ROOT, args, kwargs, failure_pattern_fn=_debugging_mod.failure_pattern_context, render_fn=_debugging_mod.render_failure_pattern_report),
            "trace":        lambda: _debugging_mod.error_trace_context(args[0] if args else "", **kwargs),
            "lookup":       lambda: _codebase_mod.framework_lookup_context(args[0] if args else "", framework_root=FRAMEWORK_ROOT, **kwargs),
            "learn":        lambda: _learn_ac(ROOT, args, {**({"project": _SESSION_STATE.active_project} if _SESSION_STATE.active_project else {}), **({"problem_id": _SESSION_STATE.active_problem} if _SESSION_STATE.active_problem else {}), **kwargs}, learning_project_id=_boot_result.project, learn_fn=_codebase_mod.learn_context, render_fn=_codebase_mod.render_learn_report, session_files_changed=_SESSION_FILES_CHANGED, session_files_created=_SESSION_FILES_CREATED, session_timings=_SESSION_TIMINGS, session_state=_SESSION_STATE, compliance_audit_fn=_compliance_audit, db_insert_audit_fn=_db_insert_audit, capture_health_fn=_capture_session_health),
            "save":         lambda: _save_with_spec(ROOT, args, kwargs),
            "confirm":      lambda: _codebase_mod.confirm_memory(ROOT, args[0] if args else "", **kwargs),
            "reject":       lambda: _codebase_mod.reject_memory(ROOT, args[0] if args else "", **kwargs),

            # ── Session & Snapshots ──
            "snapshot":     lambda: _snapshot_with_problem(*args, **kwargs),
            "save_snap":    lambda: _codebase_mod.save_snapshot(*args, root=_SESSION_STATE.artifact_root, **kwargs),
            "load_snap":    lambda: _codebase_mod.load_snapshot(*args, **kwargs),
            "archive":      lambda: _codebase_mod.archive_stale_entries(ROOT, *args, **kwargs),
            "export_md":    lambda: _codebase_mod.export_markdown(ROOT, *args, **kwargs),
            "import_md":    lambda: _codebase_mod.import_from_markdown(ROOT, *args, **kwargs),

            # ── Testing & Regression ──
            "dogfood":      lambda: _profiling_mod.dogfood_regression_context(*args, root=ROOT, **kwargs),

            # ── Diagnostics ──
            "concurrency": lambda: __import__(
                "odibi_anchor._dispatcher._concurrency", fromlist=["concurrency_action"]
            ).concurrency_action(
                *args,
                route_binding=route_binding,
                session_state=_SESSION_STATE,
                memory_db=_DEFAULT_DB_PATH,
                **kwargs,
            ),
            "db_migrate":   lambda: __import__("odibi_anchor.codebase.db_migrate_context", fromlist=["db_migrate_context"]).db_migrate_context(**kwargs),
            "memory_hygiene": lambda: __import__("odibi_anchor.codebase.memory_hygiene_context", fromlist=["memory_hygiene_context"]).memory_hygiene_context(**kwargs),
            "memory_stats": lambda: _memory_stats_impl(
                kwargs.get("db_path"), kwargs.get("output_format", "markdown"),
                project=_SESSION_STATE.active_project or "__unresolved_project__",
            ),
            "session_files": lambda: _session_files_impl(_SESSION_CONTEXT, **kwargs),
            "session_diff":  lambda: _session_diff_action_impl(ROOT, *args, **kwargs),
            "session_delta": lambda: _session_delta_context_impl(ROOT, *args, **kwargs),
            "review":       lambda: _review_with_task_scope(**kwargs),
            "status":       lambda: _status_impl(ROOT, MANIFEST, frame, _SESSION_CONTEXT, *args, **kwargs),
            "memory_tags":  lambda: _memory_tags_impl(
                *args, **{
                    **kwargs,
                    "project": _SESSION_STATE.active_project or "__unresolved_project__",
                },
            ),
            "audit_history": lambda: _audit_history_impl(ROOT, *args, **kwargs),
            "manifest":     lambda: _manifest_mod.manifest_context(ROOT, *args, **kwargs),
            "config":       lambda: _config_impl(ROOT, *args, **kwargs),
            "project":      lambda: _project_dispatch(*args, **kwargs),
            "skills":       lambda: _skills_registry_impl(ROOT, session_state=_SESSION_STATE, **kwargs),
            "references":   lambda: __import__(
                "odibi_anchor._dispatcher._references", fromlist=["reference_action"]
            ).reference_action(
                _RUNTIME_PATHS.resource_root / ".assistant" / "references", *args, **kwargs
            ),
            "tools":        lambda: _tools_action_impl(
                ROOT, ANCHOR_ROOT, _tool_registry, *args,
                _dispatch_keys=list(_dispatch.keys()),
                _action_contracts=_ACTION_CONTRACTS,
                **kwargs,
            ),
            "register_tool": lambda: _register_tool_action_impl(_tool_registry, *args, **kwargs),
            "frame":         lambda: _frame_action_impl(frame, ANCHOR_FRAME_ENABLED, *args, **kwargs),

            # ── File Tracking ──
            "touched":      lambda: _touched(args[0] if args else "", **kwargs),
            "skill_loaded": lambda: _skill_loaded_action(args[0] if args else "", **kwargs),

            # ── Session Log ──
            "log":          lambda: _log_with_attestation(*args, **kwargs),
            # get_log only accepts save_worthy_only; anchor() always injects
            # output_format, so pass through just the kwarg it understands
            # (else: TypeError: get_log() got an unexpected keyword 'output_format').
            "session_log":  lambda: get_log(save_worthy_only=kwargs.get("save_worthy_only", False)),
            "help":         lambda: _help_action_impl(args, kwargs),
            "sync":         lambda: _sync_instructions(),

            # ── Session Notebooks ──
            "new_session":  lambda: _new_session_and_store(*args, **kwargs),
            "orient":       lambda: _orient(*args, **kwargs),
            "quick":        lambda: (_ for _ in ()).throw(RuntimeError(
                "BLOCKED: anchor(\"quick\") is disabled. Use anchor(\"task\", goal=\"...\", mode=\"...\") instead.\n"
                "Every task gets full planning — no shortcuts, no exceptions."
            )),
        }

        if set(_dispatch) != set(_BUILTIN_ACTION_NAMES):
            raise AssertionError("runtime built-in dispatch surface differs from BUILTIN_ACTION_NAMES")

        if _registered_spec is not None:
            _reg_spec = _registered_spec
            if _reg_spec:
                def _exec_registered():
                    if _reg_spec.accepts_output_format and "output_format" not in kwargs:
                        kwargs["output_format"] = ANCHOR_OUTPUT_FORMAT
                    _fn = _tool_registry.resolve_callable(_reg_spec)
                    return _fn(*args, **kwargs)
                _dispatch[action] = _exec_registered

        _pre_dispatch_test_target = None
        _pre_dispatch_test_mark = None
        if action == "test":
            _pre_dispatch_test_target = kwargs.get("target") or (args[0] if args else None)
            _pre_dispatch_test_mark = kwargs.get("mark")

        _prior_task_state = None
        _prior_timing_count = len(_SESSION_TIMINGS) if action == "task" else None
        try:
            if action == "task":
                _prior_task_state = {}
                # The injected repository provider is a live process capability, not
                # transactional task state. In Databricks its SDK client retains dbutils,
                # whose __getstate__ deliberately rejects deepcopy/pickling. Pre-seeding
                # each field's memo preserves that provider both here and through any
                # prior Databricks task baseline without aliasing other rollback fields.
                _repository_provider = _SESSION_STATE.repository_provider
                for name, value in vars(_SESSION_STATE).items():
                    _task_state_memo = (
                        {id(_repository_provider): _repository_provider}
                        if _repository_provider is not None else {}
                    )
                    try:
                        _prior_task_state[name] = copy.deepcopy(value, _task_state_memo)
                    except (TypeError, ValueError):
                        # Task profiles contain immutable mappingproxy members;
                        # retaining those values by identity is safe for rollback.
                        _prior_task_state[name] = value
            _result, _err = _timed_dispatch(
                action, _dispatch, _pre_dispatch_test_target, _pre_dispatch_test_mark,
                _invocation_resolution, _pre_task_decision,
            )
            # Consumers in post-dispatch (including closure/compliance checks)
            # must see the real subcommand, never the temporary timing marker.
            if action == "learning":
                command = str(args[0]).lower().strip() if args else "list"
                if _SESSION_TIMINGS and _SESSION_TIMINGS[-1]["action"] == "learning":
                    _SESSION_TIMINGS[-1]["learning_command"] = command
            if action == "task" and _err is None:
                staged = _TASK_STAGE_holder[0] or {}
                if staged.get("continuation"):
                    # Preserve causal ordering without creating a redundant notebook.
                    _SESSION_TIMINGS.insert(max(0, len(_SESSION_TIMINGS) - 1), {
                        "action": "new_session", "elapsed_ms": 0.0, "error": None,
                        "passed": True, "synthetic": "explicit_continuation",
                    })
                    _SESSION_STATE.session_name = "continuation:" + _SESSION_STATE.session_id[:8]
                # Commit the new task window before post-dispatch can create an
                # automatic Problem Record, publish effects, or frame results.
                # A persistence failure therefore has no task-visible fallout.
                import uuid as _uuid
                _SESSION_STATE.task_window_id = "ltw_" + _uuid.uuid4().hex
                _SESSION_STATE.learning_obligation_id = None
                _SESSION_STATE.latest_closed_obligation_id = None
            from odibi_anchor._dispatcher._post_dispatch import run_post_dispatch as _run_pd
            try:
                _final = _run_pd(
                    action, _result, _err, args, kwargs,
                    session_timings=_SESSION_TIMINGS,
                    session_files_changed=_SESSION_FILES_CHANGED,
                    session_state=_SESSION_STATE,
                    planning_required_actions=_PLANNING_REQUIRED_ACTIONS,
                    task_stage=_TASK_STAGE_holder[0] if action == "task" else None,
                    invocation_resolution=_invocation_resolution,
                    pre_task_decision=_pre_task_decision,
                )
                if action == "task" and _err is None and isinstance(_final, dict):
                    from odibi_anchor.codebase._task_authority import persist_accepted_task

                    prior_window = _prior_task_state.get("task_window_id") if _prior_task_state else None
                    prepared_adoption = (_TASK_STAGE_holder[0] or {}).get("prepared_adoption")
                    if prepared_adoption is not None:
                        prior_window = prepared_adoption["prior_task_window_id"]
                    authority = persist_accepted_task(
                        _DEFAULT_DB_PATH, session_state=_SESSION_STATE,
                        task_stage=_TASK_STAGE_holder[0] or {}, task_result=_final,
                        supersede_task_window_id=prior_window,
                        prepared_adoption=prepared_adoption,
                    )
                    _final["accepted_task_authority"] = authority
                    _save_session_state({
                        "stage": "planned",
                        "files_changed": sorted(_SESSION_FILES_CHANGED),
                        "awaiting_learn": False,
                        "active_problem": _SESSION_STATE.active_problem,
                        "task_window_id": _SESSION_STATE.task_window_id,
                        "learning_obligation_id": None,
                        "latest_closed_obligation_id": None,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    })
                    from odibi_anchor._dispatcher._post_dispatch import _snapshot_durable_state

                    _snapshot_durable_state(_final, memory_db=_DEFAULT_DB_PATH)
            except Exception:
                if action == "task" and _prior_task_state is not None:
                    vars(_SESSION_STATE).clear()
                    vars(_SESSION_STATE).update(_prior_task_state)
                raise
            if action == "skill_loaded" and _err is None and isinstance(_final, dict):
                _SESSION_STATE.skills_loaded.add(str(_final["registered"]))
                _final["skills_loaded"] = sorted(_SESSION_STATE.skills_loaded)
            if action == "learning":
                command = str(args[0]).lower().strip() if args else "list"
                if command == "assess" and _err is None:
                    # Reconcile from SQLite, never from the rendered/public shape.
                    # A concurrently active row remains authoritative debt.
                    from odibi_anchor.codebase.structured_learning_context import (
                        active_learning_obligation,
                        latest_closed_learning_obligation,
                    )
                    owner_project = _SESSION_STATE.active_project or _boot_result.project
                    owner_task = _SESSION_STATE.task_window_id
                    if not owner_project or not owner_task:
                        raise RuntimeError(
                            "Learning assessment recovery requires an exact project "
                            "and task window"
                        )
                    active = active_learning_obligation(
                        project_id=owner_project, task_window_id=owner_task,
                    )
                    latest = latest_closed_learning_obligation(
                        project_id=owner_project, task_window_id=owner_task,
                    )
                    _SESSION_STATE.learning_obligation_id = (
                        active["obligation_id"] if active else None
                    )
                    _SESSION_STATE.latest_closed_obligation_id = (
                        latest["obligation_id"] if latest else None
                    )
                    _SESSION_STATE.prior_learn_debt = active is not None
                    _save_session_state({
                        "stage": "gated" if active else "learned",
                        "files_changed": sorted(_SESSION_FILES_CHANGED),
                        "awaiting_learn": active is not None,
                        "active_problem": _SESSION_STATE.active_problem,
                        "task_window_id": _SESSION_STATE.task_window_id,
                        "learning_obligation_id": _SESSION_STATE.learning_obligation_id,
                        "latest_closed_obligation_id": _SESSION_STATE.latest_closed_obligation_id,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    })
                elif command == "safe_stop" and _err is None:
                    _save_session_state({
                        "stage": _SESSION_STATE.terminal_status,
                        "files_changed": sorted(_SESSION_FILES_CHANGED),
                        "awaiting_learn": False,
                        "active_problem": _SESSION_STATE.active_problem,
                        "task_window_id": _SESSION_STATE.task_window_id,
                        "learning_obligation_id": None,
                        "latest_closed_obligation_id": _SESSION_STATE.latest_closed_obligation_id,
                        "terminal_status": _SESSION_STATE.terminal_status,
                        "terminal_basis": _SESSION_STATE.terminal_basis,
                        "terminal_reason": _SESSION_STATE.terminal_reason,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    })
            from odibi_anchor._dispatcher._operating_protocol import (
                attach_operating_protocol as _attach_protocol,
                build_operating_protocol as _build_protocol,
                should_emit_operating_protocol as _should_emit_protocol,
            )
            _routing_changed = bool(
                isinstance(_final, dict) and (
                    _final.get("routing_stale") or _final.get("routing_refreshed")
                    or _final.get("reinitialize_required")
                )
            )
            if isinstance(_final, dict) and route_binding is not None:
                if action == "status" and isinstance(_final.get("runtime"), dict):
                    _final["runtime"]["route_binding"] = route_binding_diagnostics(route_binding)
                    _final["runtime"]["routing_stale_reason"] = _routing_stale_reason[0]
                    _final["runtime"]["learning_recovery"] = (
                        _boot_result.learning_recovery_status
                    )
                    _final["runtime"]["continuity"] = {
                        "schema_version": 1,
                        "status": _SESSION_STATE.continuity_status,
                        "generation": _SESSION_STATE.continuity_generation,
                        "record_sha256": _SESSION_STATE.continuity_record_sha256,
                    }
                elif action == "orient" and isinstance(_final.get("status"), dict):
                    runtime = _final["status"].get("runtime")
                    if isinstance(runtime, dict):
                        runtime["route_binding"] = route_binding_diagnostics(route_binding)
                        runtime["routing_stale_reason"] = _routing_stale_reason[0]
                        runtime["learning_recovery"] = (
                            _boot_result.learning_recovery_status
                        )
                        runtime["continuity"] = {
                            "schema_version": 1,
                            "status": _SESSION_STATE.continuity_status,
                            "generation": _SESSION_STATE.continuity_generation,
                            "record_sha256": _SESSION_STATE.continuity_record_sha256,
                        }
            if _should_emit_protocol(
                action, args, routing_changed=_routing_changed,
                checkpoint_nested=_SESSION_STATE.checkpoint_in_progress is not None,
                kwargs=kwargs,
            ):
                _protocol = _build_protocol(
                    _SESSION_STATE, action=action,
                    result=_final if isinstance(_final, dict) else None,
                    session_timings=_SESSION_TIMINGS,
                )
                _final = _attach_protocol(_final, _protocol)
                from odibi_anchor._dispatcher._agent_context import (
                    attach_agent_context as _attach_agent_context,
                )
                _final = _attach_agent_context(
                    _final,
                    _SESSION_STATE,
                    protocol=_protocol,
                    route_binding=route_binding,
                )
            if action == "task":
                try:
                    frame = _SESSION_FRAME_holder[0]
                    if ANCHOR_FRAME_ENABLED and frame is not None and isinstance(_result, dict):
                        frame.record(action, _result)
                except Exception:
                    if _prior_task_state is not None:
                        vars(_SESSION_STATE).clear()
                        vars(_SESSION_STATE).update(_prior_task_state)
                    if _SESSION_TIMINGS and _SESSION_TIMINGS[-1]["action"] == "task":
                        _SESSION_TIMINGS[-1]["passed"] = False
                    raise
                if _err is None and isinstance(_final, dict):
                    from odibi_anchor._dispatcher._memory_actions import (
                        build_task_memory_context,
                    )
                    try:
                        _final["memory_context"] = build_task_memory_context(
                            ROOT, args, kwargs, session_state=_SESSION_STATE,
                            task_stage=_TASK_STAGE_holder[0],
                            limit=(_TASK_STAGE_holder[0] or {}).get("memory_limit", 5),
                        )
                    except Exception as exc:
                        from odibi_anchor._utils._session_state import record_degraded
                        record_degraded("task_memory_retrieval", exc)
                        _SESSION_STATE.memory_selections = []
                        _final["memory_context"] = {
                            "kind": "task_memory_context",
                            "task_window_id": _SESSION_STATE.task_window_id,
                            "selections": [],
                            "selection_count": 0,
                            "retrieval_is_application": False,
                            "authority": "advisory",
                            "unavailable_evidence": [
                                {"kind": "memory_retrieval", "reason": str(exc)[:300]}
                            ],
                        }
                    task_action_id = (
                        f"{_SESSION_STATE.session_id}:task:{_SESSION_STATE.task_window_id}"
                    )
                    _journal_action(
                        action, "start", task_action_id,
                        {"recorded_after_acceptance": True},
                    )
                    _journal_action(
                        action, "completed", task_action_id,
                        {
                            "memory_selection_count": _final["memory_context"]["selection_count"],
                            "recorded_after_acceptance": True,
                        },
                    )
        finally:
            if action == "task":
                if sys.exc_info()[0] is not None and _prior_timing_count is not None:
                    del _SESSION_TIMINGS[_prior_timing_count:]
                _TASK_STAGE_holder[0] = None
        # ── Output cost disclosure (AXI #9): teach the agent cheaper forms at
        # point of use. Centralized so every current/future action inherits it. ──
        if action not in _NO_OUTPUT_FORMAT_ACTIONS and _err is None:
            from odibi_anchor._utils._output_hints import apply_output_cost_hint
            _final = apply_output_cost_hint(action, _final, already_toon=_want_toon)

        if _deferred_markdown and isinstance(_final, dict):
            from odibi_anchor._dispatcher._operating_protocol import render_operating_protocol

            def _with_context(text):
                context = _final.get("agent_context")
                if not context:
                    return text
                from odibi_anchor._dispatcher._agent_context import render_agent_context

                return text + "\n\n" + render_agent_context(context, concise=True)

            def _with_protocol(text):
                protocol = _final.get("operating_protocol")
                rendered = (text + "\n\n" + render_operating_protocol(
                    protocol, concise=action not in {"orient", "task"},
                )) if protocol else text
                return _with_context(rendered)

            if action == "orient":
                contract = _final.get("artifact_contract", {})
                capture = _final.get("capture_standards", {})
                lines = ["# Orientation", "", str(_final.get("summary", "")), "",
                         f"## Managed artifact contract v{contract.get('version', 'unknown')}",
                         "", str(contract.get("root_rule", "")), ""]
                lines.extend(
                    f"- `{item.get('path')}` — {item.get('use_when', '')} "
                    f"Do not use for: {item.get('do_not_use_for', '')}"
                    for item in contract.get("artifacts", [])
                )
                if contract.get("activation"):
                    lines.extend(["", str(contract["activation"])])
                if capture.get("version"):
                    from odibi_anchor._dispatcher._capture_standards import (
                        render_capture_standards_markdown,
                    )
                    lines.extend(["", render_capture_standards_markdown(capture)])
                return _with_protocol("\n".join(lines))
            if action == "task":
                from odibi_anchor.planning import render_task_execution_report
                # The public task renderer owns the complete task representation,
                # including protocol, so dispatcher markdown must not append it.
                return _with_context(render_task_execution_report(_final))
            if action == "snapshot":
                from odibi_anchor.planning.handoff_context import render_handoff_report

                rendered = render_handoff_report(_final)
                if "authority_sha256" in _final and "first_action" in _final:
                    rendered += (
                        "\n## Canonical authority\n\n"
                        f"Digest: `{_final['authority_sha256']}`\n"
                        f"First action: `{_final['first_action']['copy_ready']}`\n"
                    )
                return _with_protocol(rendered)
            if action == "problem":
                from odibi_anchor._dispatcher._problem import render_problem_result
                return _with_protocol(render_problem_result(str(args[0]).lower().strip() if args else "list", _final))
            if action == "work_item":
                from odibi_anchor._dispatcher._work_item import render_work_item_result
                return _with_protocol(render_work_item_result(str(args[0]).lower().strip() if args else "list", _final))
            if action == "spec":
                from odibi_anchor._dispatcher._spec import render_spec_result
                return _with_protocol(render_spec_result(str(args[0]).lower().strip() if args else "list", _final))
            if action == "project":
                from odibi_anchor._dispatcher._project import _render_project_context
                return _with_protocol(_render_project_context(_final))
            if action == "preflight":
                from odibi_anchor.codebase.preflight_context import render_preflight_report
                return render_preflight_report(_final)
            if action == "test":
                from odibi_anchor._dispatcher._session_tools import _format_test_result
                return _format_test_result(_final, "markdown")
            if action == "gate":
                from odibi_anchor.codebase.workflow_gate_context import render_workflow_gate_report
                return _with_protocol(render_workflow_gate_report(_final))
            if action == "checkpoint":
                from odibi_anchor._dispatcher._checkpoint import format_checkpoint
                return _with_protocol(format_checkpoint(_final, "markdown"))
            if action in {"learn", "learning"}:
                import json as _json
                title = "Structured Learning" if action == "learning" else "Learning"
                payload = {key: value for key, value in _final.items()
                           if key not in {"operating_protocol", "agent_context"}}
                return _with_protocol(f"# {title}\n\n```json\n" + _json.dumps(payload, indent=2) + "\n```")
            if action in {
                "incident_snapshot", "environment_diff", "spark_diagnose", "uc_context",
                "delta_changes", "run_diff", "observe_table", "table_trend",
            }:
                from odibi_anchor.operational import render_operational_markdown
                return render_operational_markdown(action, _final)
        if _want_toon and isinstance(_final, dict):
            from odibi_anchor._utils._toon import render_toon
            return render_toon(_final)
        return _final

    # ── Run the boot sequence ────────────────────────────────────────────────
    from odibi_anchor._dispatcher._boot import run_boot as _run_boot

    _boot_result = _run_boot(
        root=str(ROOT),
        anchor_root=ANCHOR_ROOT,
        state_root=_SESSION_STATE.artifact_root,
        project_id=_SESSION_STATE.active_project,
        db_path=_DEFAULT_DB_PATH,
        boot_memory_limit=_BOOT_MEMORY_LIMIT,
        frame_enabled=ANCHOR_FRAME_ENABLED,
        rebind_task=rebind_task,
        route_binding=route_binding,
    )
    _SESSION_STATE.learning_owner_project_id = (
        _boot_result.project if _boot_result.project != "unknown" else None
    )

    MANIFEST = _boot_result.manifest
    _SESSION_FRAME_holder[0] = _boot_result.frame

    def _operating_protocol_snapshot() -> dict:
        """Return a side-effect-free lifecycle projection for transport adapters."""
        from odibi_anchor._dispatcher._operating_protocol import build_operating_protocol
        return build_operating_protocol(
            _SESSION_STATE, action="snapshot", session_timings=_SESSION_TIMINGS,
        )

    def _agent_context_snapshot() -> dict:
        """Return compact context from the same side-effect-free lifecycle snapshot."""
        from odibi_anchor._dispatcher._agent_context import build_agent_context

        protocol = _operating_protocol_snapshot()
        return cast(dict, build_agent_context(
            _SESSION_STATE,
            protocol=protocol,
            route_binding=route_binding,
            view="compact",
        ))

    anchor.__dict__.update({
        "_operating_protocol_snapshot": _operating_protocol_snapshot,
        "_agent_context_snapshot": _agent_context_snapshot,
        "_task_rebind_result": _boot_result.task_rebind,
        "_route_binding": route_binding,
    })

    return anchor, ROOT, MANIFEST
