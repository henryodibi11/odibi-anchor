"""Shared session state singleton for odibi_anchor.

This module provides mutable session state that needs to be shared between
the exec'd agent_init.py namespace and modules that `import agent_init`.

Problem solved:
    agent_init.py is both exec'd (bootstrap into notebook globals) AND importable
    (by safe_change_context.py). When exec'd, mutable state defined in agent_init.py
    exists in the NOTEBOOK globals. When imported, a SEPARATE module object is created
    with its own copies. This split means safe_change_context's auto-touched writes
    to an orphan set that anchor("gate") never reads.

Solution:
    By placing shared state HERE (in a proper package module), both the exec'd
    namespace and import-based modules resolve to the SAME sys.modules entry.
    The mutable objects (sets, lists) are truly shared.

Usage:
    # In agent_init.py (exec'd):
    from odibi_anchor._utils._session_state import (
        _SESSION_FILES_CHANGED, _SESSION_FILES_CREATED, _SESSION_TIMINGS,
        _SESSION_LOG,
        touched, get_state, record_timing, log_note, get_log,
    )

    # In safe_change_context.py (imported):
    from odibi_anchor._utils._session_state import touched as _session_touched
"""

from __future__ import annotations

import contextlib as _contextlib
import os as _os
import uuid as _uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime as _datetime
from datetime import timezone as _timezone
from pathlib import PurePosixPath as _PurePosixPath
from typing import Any

from odibi_anchor._utils.ast_utils import ast_cache_clear
from odibi_anchor._utils.ast_utils import ast_cache_invalidate as _ast_invalidate

# ─── SessionState dataclass ───────────────────────────────────────────────────
# Encapsulates agent_init-specific mutable scalars that previously required
# `global` declarations inside anchor(). By holding them as attributes of a shared
# instance, anchor() can mutate them via attribute assignment without `global`.

@dataclass
class SessionState:
    """Mutable session state for the anchor() dispatcher.

    Instantiated once at boot. Shared via sys.modules singleton pattern
    (same as _SESSION_FILES_CHANGED etc. above in this module).

    Attributes:
        files_at_last_checkpoint: File count at last successful gate/checkpoint.
            Used to enforce the checkpoint-between-features rule.
        prior_learn_debt: True if the previous session ended without anchor("learn").
            Blocks anchor("task") until debt is cleared.
        boot_manifest: Hash manifest loaded at boot for filesystem drift detection.
            Maps relative file paths to {"sha256": "..."} entries.
    """
    files_at_last_checkpoint: int = 0
    prior_learn_debt: bool = False
    checkpoint_in_progress: dict | None = None  # Shared marker visible to nested learn/audit.
    boot_manifest: dict | None = None
    notebook_path: str | None = None
    active_spec: dict | None = None  # Parsed spec from anchor("task", spec="...")
    active_task_mode: str | None = None  # Mode from last anchor("task") — used by spec/skill gates
    active_task_profile: Any | None = None  # Canonical immutable TaskProfile from the last task.
    active_assurance_plan: Any | None = None  # Immutable advisory assurance plan for the accepted task.
    bps_kernel: Any | None = None  # Universal in-memory BPS framing for the accepted task.
    linked_problem: str | None = None  # Problem linked to the accepted task (not session history).
    linked_spec: str | None = None  # Spec linked to the accepted task (not learning lineage).
    linked_work_item: str | None = None  # Optional exact WI-* authority supplied to the task.
    pending_task_spec_name: str | None = None  # One-shot binding from successful spec execute.
    explicit_problem_requested: bool = False
    explicit_spec_requested: bool = False
    explicit_pr_draft_requested: bool | None = None
    current_phase: int = 1
    phase_count: int = 1
    referenced_facts: tuple[str, ...] = ()
    evidence_ledger: list[Any] = field(default_factory=list)
    managed_artifact_ledger: list[Any] = field(default_factory=list)
    guidance_attestations: list[Any] = field(default_factory=list)
    observed_effects: list[str] = field(default_factory=list)
    intended_pr_paths: tuple[str, ...] = ()
    repository_snapshot: Any | None = None
    repository_provider: Any | None = None
    task_repository_baseline: Any | None = None
    task_repository_baseline_qualification: Any | None = None
    task_handoff_context: dict[str, Any] = field(default_factory=dict)
    task_repository_write_fingerprints: dict[str, str] = field(default_factory=dict)
    task_verification_epoch: int | None = None
    pre_task_task_required_attempts: int = 0
    repository_pr_config: dict[str, Any] | None = None
    spec_persisted: bool = False  # Set True when anchor("spec", "persist") succeeds
    persisted_spec_name: str | None = None  # Canonical persisted spec bound to linked_spec.
    active_spec_name: str | None = None  # Name of the active spec — links learnings (S-4)
    active_problem: str | None = None  # Stable PRB-* ID for the current investigation.
    spec_review_rating: str | None = None  # Rating from anchor("spec", "review"): excellent/good/needs-work/poor
    reviewed_spec_name: str | None = None  # Canonical name owning spec_review_rating.
    skills_loaded: set = field(default_factory=set)  # Skill names loaded via anchor("skill_loaded", "name")
    anchor_home: str | None = None  # Package-derived Odibi Anchor repository.
    active_project: str | None = None  # Managed project ID selected beneath workspace/projects.
    project_root: str | None = None  # Managed project directory, independent of external target.
    artifact_root: str | None = None  # Canonical destination for project artifacts.
    target_root: str | None = None  # Source tree used by code-inspection/edit actions.
    routing_stale: bool = False  # Direct dispatcher must be rebound after project routing changes.
    route_schema_version: str | None = None
    route_fingerprint: str | None = None
    runtime_instance_id: str | None = None
    trust_domain: str | None = None
    learning_owner_project_id: str | None = None
    continuity_generation: int = 0
    continuity_record_sha256: str | None = None
    continuity_status: str = "uninitialized"
    session_id: str = field(default_factory=lambda: str(_uuid.uuid4()))
    task_window_id: str = field(default_factory=lambda: "ltw_" + _uuid.uuid4().hex)
    learning_obligation_id: str | None = None
    latest_closed_obligation_id: str | None = None
    terminal_status: str | None = None
    terminal_basis: str | None = None
    terminal_reason: str | None = None
    session_name: str | None = None
    task_goal: str | None = None
    task_tags: list[str] = field(default_factory=list)
    memory_selections: list[str] = field(default_factory=list)
    memory_dispositions: list[str] = field(default_factory=list)
    memory_applications: list[str] = field(default_factory=list)
    memory_evaluations: list[str] = field(default_factory=list)
    audit_epoch: str = "delivery-v2"

    def __post_init__(self) -> None:
        """Restore optional assurance plans without making legacy state unusable."""
        value = self.active_assurance_plan
        if value is None:
            return
        try:
            from odibi_anchor.assurance import AssurancePlan

            payload = value if isinstance(value, Mapping) else value.to_dict()
            self.active_assurance_plan = AssurancePlan.from_dict(payload)
        except (AttributeError, TypeError, ValueError):
            self.active_assurance_plan = None


# Module-level singleton — shared between exec'd and imported code.
_SESSION_STATE = SessionState()



# ─── Shared mutable session state ────────────────────────────────────────────
# These objects are the SINGLE source of truth. Both exec'd and imported code
# reference the same set/list instances via this module.

_SESSION_FILES_CHANGED: set[str] = set()
_SESSION_FILES_CREATED: set[str] = set()
_SESSION_TIMINGS: list[dict] = []
_SESSION_DIFF_BASELINES: dict[str, str] = {}  # file_path -> pre-edit content for diff
_SESSION_PROVEN_CREATED: set[str] = set()  # write guard proved path absent from boot state
_SESSION_PROVEN_BASELINES: dict[str, str] = {}  # guard baseline matched boot identity
_SESSION_LOG: list[dict] = []
_SESSION_DEGRADED: list[dict] = []  # non-fatal silent failures, surfaced in anchor("status")


def record_degraded(component: str, reason: object) -> None:
    """Record a non-fatal failure that was otherwise swallowed (AXI principle #6).

    Lets the system keep running (boot/compliance/auto-confirm must never crash)
    while making the degradation VISIBLE — anchor("status") surfaces the count so the
    agent knows it may be operating on partial state.
    """
    _SESSION_DEGRADED.append({"component": str(component), "reason": str(reason)[:300]})


def get_degraded() -> list[dict]:
    """Return recorded non-fatal failures this session."""
    return list(_SESSION_DEGRADED)

# Scratch directories — free to edit without permission
_SCRATCH_DIRS: frozenset = frozenset({"_scratch"})


def canonical_session_path(path: str, root: str = "") -> str:
    """Return one root-relative, forward-slash key for session file state."""
    candidate = path.replace("\\", _os.sep).replace("/", _os.sep)
    if root:
        root_abs = _os.path.abspath(root)
        abs_path = candidate if _os.path.isabs(candidate) else _os.path.join(root_abs, candidate)
        abs_path = _os.path.abspath(abs_path)
        try:
            if _os.path.commonpath([abs_path, root_abs]) == root_abs:
                candidate = _os.path.relpath(abs_path, root_abs)
            else:
                candidate = abs_path
        except ValueError:
            candidate = abs_path
    return _os.path.normpath(candidate).replace(_os.sep, "/")


def touched(path: str, *, created: bool = False, root: str = "") -> dict:
    """Register a file as changed (or created) during this session.

    Shared implementation used by both the anchor() dispatcher and safe_change_context.
    Automatically invalidates AST cache and runs syntax check on .py files.

    Args:
        path: Relative or absolute path to the file. Must be non-empty.
        created: If True, also register in the created-files set.
        root: Project root for resolving relative paths.

    Returns:
        Dict with registration info and syntax_check result.

    Raises:
        ValueError: If path is empty.
    """
    if not path or not path.strip():
        raise ValueError("path must be a non-empty string")

    if is_managed_artifact_path(path, artifact_root=_SESSION_STATE.artifact_root,
                                target_root=_SESSION_STATE.target_root or root):
        entry = record_managed_artifact(_SESSION_STATE, path, "managed-artifact", source="anchor:touched")
        return {"registered": path, "created": created, "managed_artifact": entry.to_dict(),
                "session_changed": len(_SESSION_FILES_CHANGED), "session_created": len(_SESSION_FILES_CREATED),
                "syntax_check": "n/a", "test_hint": None, "memory_hint": None, "review_hint": None,
                "doc_hint": None}

    path = canonical_session_path(path, root)

    # A caller-declared created file has no pre-edit content. Do not snapshot the
    # already-written file against itself or review will report it as unchanged.
    if created:
        _SESSION_DIFF_BASELINES.pop(path, None)
    elif path not in _SESSION_DIFF_BASELINES:
        try:
            abs_path = path if _os.path.isabs(path) else _os.path.join(root, path) if root else path
            if _os.path.exists(abs_path):
                with open(abs_path, encoding="utf-8") as _bf:
                    _SESSION_DIFF_BASELINES[path] = _bf.read()
        except (OSError, UnicodeDecodeError):
            pass  # Binary or unreadable — skip baseline

    _SESSION_FILES_CHANGED.add(path)
    if created:
        _SESSION_FILES_CREATED.add(path)

    # Auto-invalidate AST cache so next map/impact/preflight re-parses
    with _contextlib.suppress(Exception):
        _ast_invalidate(path)

    # Auto-preflight: lightweight syntax check on .py files
    syntax_ok = None
    syntax_error = None
    if path.endswith(".py"):
        try:
            abs_path = path if _os.path.isabs(path) else _os.path.join(root, path) if root else path
            if _os.path.exists(abs_path):
                with open(abs_path, encoding="utf-8") as f:
                    source = f.read()
                compile(source, abs_path, "exec")
                syntax_ok = True
            else:
                syntax_ok = None  # File not found — skip check
        except SyntaxError as se:
            syntax_ok = False
            syntax_error = f"Line {se.lineno}: {se.msg}"

    result: dict[str, Any] = {
        "registered": path,
        "created": created,
        "session_changed": len(_SESSION_FILES_CHANGED),
        "session_created": len(_SESSION_FILES_CREATED),
    }
    if syntax_ok is not None:
        result["syntax_check"] = "passed" if syntax_ok else "FAILED"
        if syntax_error:
            result["syntax_error"] = syntax_error
    elif not path.endswith(".py"):
        result["syntax_check"] = "n/a"  # Non-.py files don't get syntax checks
    # .py files that don't exist: omit syntax_check (None case)

    # Test enforcement hint for .py files
    result["test_hint"] = "MUST: Ensure test coverage for this file. Load skills/writing-tests/SKILL.md when authoring tests." if path.endswith(".py") else None

    # Reinforce the accepted task's bounded review without widening retrieval.
    result["memory_hint"] = (
        "MUST: Dispose every accepted task-memory selection before learning assessment; "
        "evaluate any selection that influenced this change."
    )

    # Self-review hint for .py files
    result["review_hint"] = "SHOULD: Before anchor('gate'), run anchor('diff') and review complete changeset." if path.endswith(".py") else None

    # Documentation trigger for new files
    result["doc_hint"] = "MUST: Add module-level docstring and function docstrings. Load skills/documentation/SKILL.md for documentation work." if created and path.endswith(".py") else ("MUST: Update CHANGELOG.md for new file." if created else None)

    return result


def is_managed_artifact_path(path: str, *, artifact_root: str | None, target_root: str | None) -> bool:
    """Classify only exact managed allowlisted paths, especially for colocated roots."""
    if not artifact_root:
        return False
    absolute = _os.path.realpath(path if _os.path.isabs(path) else _os.path.join(target_root or artifact_root, path))
    artifact = _os.path.realpath(artifact_root)
    try:
        relative = _os.path.relpath(absolute, artifact).replace(_os.sep, "/")
    except ValueError:
        return False
    if relative == ".." or relative.startswith("../"):
        return False
    first = relative.split("/", 1)[0]
    from odibi_anchor._dispatcher._project import managed_artifact_root_names
    return first in managed_artifact_root_names()


def record_managed_artifact(session_state: SessionState, path: str, kind: str, *, source: str,
                            provenance: dict[str, Any] | None = None):
    """Append a typed managed artifact without touching the source ledger."""
    from odibi_anchor.planning._task_policy import ManagedArtifactEntry
    entry = ManagedArtifactEntry(path, kind, _datetime.now(_timezone.utc).replace(microsecond=0).isoformat(),
                                 {"source": source, **(provenance or {})})
    if not hasattr(session_state, "managed_artifact_ledger"):
        session_state.managed_artifact_ledger = []
    session_state.managed_artifact_ledger.append(entry)
    return entry


def record_evidence(session_state: SessionState, *, id: str, kind: str, status: str,
                    source: str, observed_at: str | None = None,
                    provenance: dict[str, Any] | None = None):
    """Append an explicitly accepted mechanical or attested observation."""
    from odibi_anchor.planning._task_policy import EvidenceEntry
    entry = EvidenceEntry(id, kind, status, source,
                          observed_at or _datetime.now(_timezone.utc).replace(microsecond=0).isoformat(),
                          provenance or {})
    if not hasattr(session_state, "evidence_ledger"):
        session_state.evidence_ledger = []
    for index, existing in enumerate(session_state.evidence_ledger):
        if existing.id == id:
            session_state.evidence_ledger[index] = entry
            return entry
    session_state.evidence_ledger.append(entry)
    return entry


def record_guidance_attestation(session_state: SessionState, *, id: str, kind: str,
                                status: str, source: str, attestor: str,
                                observed_at: str | None = None,
                                provenance: dict[str, Any] | None = None):
    """Record a caller-authored attestation in the dedicated guidance lane."""
    from odibi_anchor.planning._task_policy import EvidenceEntry
    entry = EvidenceEntry(
        id, kind, status, source,
        observed_at or _datetime.now(_timezone.utc).replace(microsecond=0).isoformat(),
        {"attestor": attestor, **(provenance or {})},
    )
    for index, existing in enumerate(session_state.guidance_attestations):
        if existing.id == id:
            session_state.guidance_attestations[index] = entry
            return entry
    session_state.guidance_attestations.append(entry)
    return entry


def reset_task_policy_state(session_state: SessionState) -> None:
    """Clear all task/profile policy state while preserving runtime roots."""
    session_state.active_task_mode = None
    session_state.active_task_profile = None
    session_state.active_assurance_plan = None
    session_state.task_goal = None
    session_state.task_tags = []
    session_state.memory_selections = []
    session_state.memory_dispositions = []
    session_state.memory_applications = []
    session_state.memory_evaluations = []
    session_state.bps_kernel = None
    session_state.linked_problem = None
    session_state.linked_spec = None
    session_state.linked_work_item = None
    session_state.pending_task_spec_name = None
    session_state.explicit_problem_requested = False
    session_state.explicit_spec_requested = False
    session_state.explicit_pr_draft_requested = None
    session_state.current_phase = 1
    session_state.phase_count = 1
    session_state.referenced_facts = ()
    session_state.evidence_ledger = []
    session_state.managed_artifact_ledger = []
    session_state.guidance_attestations = []
    session_state.observed_effects = []
    session_state.intended_pr_paths = ()
    session_state.repository_snapshot = None
    session_state.task_repository_baseline = None
    session_state.task_repository_baseline_qualification = None
    session_state.task_handoff_context = {}
    session_state.task_repository_write_fingerprints = {}
    session_state.task_verification_epoch = None
    session_state.pre_task_task_required_attempts = 0
    session_state.repository_pr_config = None
    session_state.active_spec = None
    session_state.spec_persisted = False
    session_state.persisted_spec_name = None
    session_state.active_spec_name = None
    session_state.active_problem = None
    session_state.spec_review_rating = None
    session_state.reviewed_spec_name = None
    session_state.terminal_status = None
    session_state.terminal_basis = None
    session_state.terminal_reason = None


def record_timing(action: str, elapsed_ms: float, error: str | None = None) -> None:
    """Record a timed action in the session registry."""
    _SESSION_TIMINGS.append({
        "action": action,
        "elapsed_ms": round(elapsed_ms, 1),
        "error": error,
    })


def get_state() -> dict[str, Any]:
    """Return current session state snapshot."""
    from dataclasses import asdict, is_dataclass

    task_baseline = _SESSION_STATE.task_repository_baseline
    baseline_qualification = _SESSION_STATE.task_repository_baseline_qualification
    if getattr(task_baseline, "evidence_kind", None) == "databricks_git_folder":
        from odibi_anchor._repository_snapshot import databricks_task_baseline_projection
        task_baseline = databricks_task_baseline_projection(task_baseline)
    return {
        "session_id": _SESSION_STATE.session_id,
        "session_name": _SESSION_STATE.session_name,
        "project": _SESSION_STATE.active_project,
        "task_goal": _SESSION_STATE.task_goal,
        "active_task_profile": (
            _SESSION_STATE.active_task_profile.to_dict() if _SESSION_STATE.active_task_profile else None
        ),
        "active_assurance_plan": (
            _SESSION_STATE.active_assurance_plan.to_dict()
            if _SESSION_STATE.active_assurance_plan is not None else None
        ),
        "bps_kernel": _SESSION_STATE.bps_kernel.to_dict() if _SESSION_STATE.bps_kernel else None,
        "referenced_facts": list(_SESSION_STATE.referenced_facts),
        "evidence_ledger": [entry.to_dict() for entry in _SESSION_STATE.evidence_ledger],
        "managed_artifact_ledger": [entry.to_dict() for entry in _SESSION_STATE.managed_artifact_ledger],
        "guidance_attestations": [entry.to_dict() for entry in _SESSION_STATE.guidance_attestations],
        "observed_effects": list(_SESSION_STATE.observed_effects),
        "intended_pr_paths": list(_SESSION_STATE.intended_pr_paths),
        "repository_snapshot": _SESSION_STATE.repository_snapshot,
        "repository_provider_id": getattr(_SESSION_STATE.repository_provider, "provider_id", None),
        "task_repository_baseline": task_baseline,
        "task_repository_baseline_qualification": (
            asdict(baseline_qualification)
            if is_dataclass(baseline_qualification) and not isinstance(baseline_qualification, type)
            else baseline_qualification
        ),
        "task_handoff_context": _SESSION_STATE.task_handoff_context,
        "repository_pr_config": _SESSION_STATE.repository_pr_config,
        "tags": list(_SESSION_STATE.task_tags),
        "memory_selections": list(_SESSION_STATE.memory_selections),
        "memory_dispositions": list(_SESSION_STATE.memory_dispositions),
        "memory_applications": list(_SESSION_STATE.memory_applications),
        "memory_evaluations": list(_SESSION_STATE.memory_evaluations),
        "audit_epoch": _SESSION_STATE.audit_epoch,
        "project_root": _SESSION_STATE.project_root,
        "artifact_root": _SESSION_STATE.artifact_root,
        "target_root": _SESSION_STATE.target_root,
        "files_changed": sorted(_SESSION_FILES_CHANGED),
        "files_created": sorted(_SESSION_FILES_CREATED),
        "timings": _SESSION_TIMINGS,
        "total_changed": len(_SESSION_FILES_CHANGED),
        "total_created": len(_SESSION_FILES_CREATED),
        "total_actions": len(_SESSION_TIMINGS),
        "total_time_ms": round(sum(t["elapsed_ms"] for t in _SESSION_TIMINGS), 1),
    }


def is_empty() -> bool:
    """Return True if no session state has been recorded."""
    return not _SESSION_FILES_CHANGED and not _SESSION_FILES_CREATED and not _SESSION_TIMINGS


def get_diff(target: str | None = None, stat: bool = False, root: str = "") -> dict[str, Any]:
    """Compute session diff against baselines.

    Args:
        target: Optional single file to diff. If None, diffs all changed files.
        stat: If True, return only diffstat summary (no content).
        root: Project root for resolving paths.

    Returns:
        Dict with kind='session_diff', per-file diffs or stats.
    """
    import difflib

    files_to_diff = [canonical_session_path(target, root)] if target else sorted(_SESSION_FILES_CHANGED)
    per_file_diffs: dict[str, Any] = {}
    total_add = 0
    total_del = 0

    for path in files_to_diff:
        abs_path = path if _os.path.isabs(path) else _os.path.join(root, path) if root else path
        baseline = _SESSION_DIFF_BASELINES.get(path)

        # Read current content
        try:
            if _os.path.exists(abs_path):
                with open(abs_path, encoding="utf-8") as f:
                    current = f.read()
            else:
                current = ""  # File deleted
        except (OSError, UnicodeDecodeError):
            per_file_diffs[path] = {"error": "unreadable"}
            continue

        if baseline is None:
            # New file — entire content is an addition
            lines = current.split("\n")
            total_add += len(lines)
            if stat:
                per_file_diffs[path] = {"additions": len(lines), "deletions": 0, "status": "new"}
            else:
                per_file_diffs[path] = {
                    "status": "new",
                    "additions": len(lines),
                    "deletions": 0,
                    "diff": f"+++ {path} (new file, {len(lines)} lines)",
                }
            continue

        # Compute unified diff
        baseline_lines = baseline.splitlines()
        current_lines = current.splitlines()

        diff_lines = list(difflib.unified_diff(
            baseline_lines, current_lines,
            fromfile=f"a/{path}", tofile=f"b/{path}",
            lineterm="",
        ))

        adds = sum(1 for line in diff_lines if line.startswith("+") and not line.startswith("+++"))
        dels = sum(1 for line in diff_lines if line.startswith("-") and not line.startswith("---"))
        total_add += adds
        total_del += dels

        if stat:
            per_file_diffs[path] = {"additions": adds, "deletions": dels, "status": "modified"}
        else:
            # Truncate per-file diff at 200 lines unless focused
            diff_text = "\n".join(diff_lines[:200] if not target else diff_lines)
            if len(diff_lines) > 200 and not target:
                diff_text += f"\n... ({len(diff_lines) - 200} more lines)"
            per_file_diffs[path] = {
                "status": "modified" if diff_lines else "unchanged",
                "additions": adds,
                "deletions": dels,
                "diff": diff_text,
            }

    return {
        "kind": "session_diff",
        "subject": target or "session",
        "summary": f"{len(per_file_diffs)} file(s): +{total_add}/-{total_del} lines",
        "metrics": {
            "files_changed": len(per_file_diffs),
            "total_additions": total_add,
            "total_deletions": total_del,
            "net_lines": total_add - total_del,
        },
        "findings": [],
        "risks": [],
        "samples": {"per_file": per_file_diffs} if not stat else {"diffstat": per_file_diffs},
        "suggested_next_actions": [
            "MUST: Commit changes via Repos UI.",
            "MUST: Capture genuine findings with anchor('learning', 'capture', ...), then close with anchor('learning', 'assess', ...).",
        ],
    }


def log_note(action: str, finding: str, *, save_worthy: bool = False) -> dict:
    """Append a timestamped note to the session log.

    Call after significant actions to prevent context degradation.
    At session end, reference log when deciding what to save to memory.
    """
    note: dict[str, Any] = {
        "timestamp": _datetime.now(_timezone.utc).isoformat(),
        "action": action,
        "finding": finding,
        "save_worthy": save_worthy,
    }
    _SESSION_LOG.append(note)
    return note


def get_log(*, save_worthy_only: bool = False) -> list[dict]:
    """Return session log, optionally filtered to save-worthy entries."""
    if save_worthy_only:
        return [n for n in _SESSION_LOG if n["save_worthy"]]
    return list(_SESSION_LOG)


def is_scratch_path(path: str) -> bool:
    """Check if path is in a scratch directory (free to edit without permission)."""
    parts = _PurePosixPath(path.replace(_os.sep, "/")).parts
    return bool(_SCRATCH_DIRS & set(parts))


# ─── Boot manifest for filesystem drift detection ────────────────────────────

_SESSION_BOOT_MANIFEST: dict[str, dict] = {}
_BOOT_MANIFEST_ROOT: str | None = None
_BOOT_MANIFEST_BUILT: bool = False

_GOVERNED_EXTENSIONS: frozenset[str] = frozenset({
    ".py", ".sql", ".scala", ".yaml", ".yml", ".json", ".toml", ".md", ".txt",
})

_SKIP_DIRS: frozenset[str] = frozenset({
    "__pycache__", ".venv", ".git", ".pytest_cache", "node_modules",
    "_scratch",
})

_CW_GENERATED_FILES: frozenset[str] = frozenset({
    ".anchor_session_state.json", ".anchor_config.json", ".patch_log.jsonl",
    ".agent_memory.db", ".agent_memory.db-journal", ".agent_memory.db-wal",
})


def build_boot_manifest(root: str) -> dict[str, dict]:
    """Record the project root for deferred manifest building.

    Instead of walking the filesystem at boot (slow on FUSE-mounted /Workspace),
    this defers the actual stat scan to the first call to
    :func:`check_filesystem_drift`. At boot, only the root is recorded.

    Args:
        root: Absolute path to the project root directory.

    Returns:
        Empty dict (manifest is built lazily on first drift check).
    """
    global _SESSION_BOOT_MANIFEST, _BOOT_MANIFEST_ROOT, _BOOT_MANIFEST_BUILT
    _BOOT_MANIFEST_ROOT = root
    _BOOT_MANIFEST_BUILT = False
    _SESSION_BOOT_MANIFEST = {}
    return _SESSION_BOOT_MANIFEST


def _ensure_boot_manifest_built() -> None:
    """Build the boot manifest on first demand (lazy initialization).

    Called by :func:`check_filesystem_drift` on its first invocation.
    Uses mtime + size from os.stat() for comparison.
    """
    global _SESSION_BOOT_MANIFEST, _BOOT_MANIFEST_BUILT
    if _BOOT_MANIFEST_BUILT or not _BOOT_MANIFEST_ROOT:
        return

    manifest: dict[str, dict] = {}
    root = _BOOT_MANIFEST_ROOT

    for dirpath, dirnames, filenames in _os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]

        for fname in filenames:
            if fname in _CW_GENERATED_FILES:
                continue
            ext = _os.path.splitext(fname)[1].lower()
            if ext not in _GOVERNED_EXTENSIONS:
                continue

            abs_path = _os.path.join(dirpath, fname)
            rel_path = _os.path.relpath(abs_path, root).replace(_os.sep, "/")

            try:
                st = _os.stat(abs_path)
                manifest[rel_path] = {"mtime": st.st_mtime, "size": st.st_size}
            except (OSError, PermissionError):
                continue

    _SESSION_BOOT_MANIFEST = manifest
    _BOOT_MANIFEST_BUILT = True


def reconcile_session_file_ledger(root: str) -> dict[str, list[str]]:
    """Discard only session file entries whose net filesystem change is zero."""
    removed_created: list[str] = []
    removed_restored: list[str] = []

    for path in list(_SESSION_FILES_CHANGED):
        abs_path = path if _os.path.isabs(path) else _os.path.join(root, path)
        was_created = path in _SESSION_PROVEN_CREATED
        if not _os.path.exists(abs_path) and was_created:
            _SESSION_FILES_CREATED.discard(path)
            _SESSION_PROVEN_CREATED.discard(path)
            _SESSION_FILES_CHANGED.discard(path)
            _SESSION_DIFF_BASELINES.pop(path, None)
            _SESSION_PROVEN_BASELINES.pop(path, None)
            removed_created.append(path)

    for path in list(_SESSION_FILES_CHANGED - _SESSION_PROVEN_CREATED):
        baseline = _SESSION_PROVEN_BASELINES.get(path)
        if baseline is None:
            continue
        abs_path = path if _os.path.isabs(path) else _os.path.join(root, path)
        if not _os.path.isfile(abs_path):
            continue  # A pre-existing deleted file remains a real change.
        try:
            with open(abs_path, encoding="utf-8") as current_file:
                current = current_file.read()
        except (OSError, UnicodeDecodeError):
            continue
        if current == baseline:
            _SESSION_FILES_CHANGED.discard(path)
            _SESSION_DIFF_BASELINES.pop(path, None)
            _SESSION_PROVEN_BASELINES.pop(path, None)
            if path in _SESSION_BOOT_MANIFEST:
                try:
                    stat = _os.stat(abs_path)
                    _SESSION_BOOT_MANIFEST[path] = {"mtime": stat.st_mtime, "size": stat.st_size}
                except OSError:
                    pass
            removed_restored.append(path)

    return {"removed_created": removed_created, "removed_restored": removed_restored}


def check_filesystem_drift(root: str) -> dict[str, Any]:
    """Compare current filesystem state against the boot manifest.

    Detects files that have been modified, created, or deleted since
    :func:`build_boot_manifest` was called. Uses mtime + size comparison
    for speed. Files that appear in the drift but are NOT registered via
    :func:`touched` are flagged as *unregistered*.

    Args:
        root: Absolute path to the project root directory.

    Returns:
        Dict with keys ``modified``, ``created``, ``deleted``,
        ``unregistered``, and ``has_drift``.  If no boot manifest exists,
        returns a graceful-degradation dict instead.
    """
    # Lazy: build manifest on first drift check if not yet done
    _ensure_boot_manifest_built()

    if not _SESSION_BOOT_MANIFEST:
        return {"has_drift": False, "error": "no boot manifest"}

    current = _build_current_manifest(root)

    boot_keys = set(_SESSION_BOOT_MANIFEST)
    current_keys = set(current)

    modified = [
        p for p in boot_keys & current_keys
        if (current[p]["mtime"] != _SESSION_BOOT_MANIFEST[p]["mtime"]
            or current[p]["size"] != _SESSION_BOOT_MANIFEST[p]["size"])
    ]
    created = sorted(current_keys - boot_keys)
    deleted = sorted(boot_keys - current_keys)

    # Flag both modified AND created files as unregistered drift if not
    # registered via touched() — catches all unauthorized filesystem changes.
    unregistered = sorted((set(modified) | set(created)) - _SESSION_FILES_CHANGED)

    return {
        "modified": sorted(modified),
        "created": created,
        "deleted": deleted,
        "unregistered": unregistered,
        "has_drift": len(unregistered) > 0,
    }


def _build_current_manifest(root: str) -> dict[str, dict]:
    """Build a manifest of the current filesystem state WITHOUT overwriting the boot manifest."""
    manifest: dict[str, dict] = {}

    for dirpath, dirnames, filenames in _os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]

        for fname in filenames:
            if fname in _CW_GENERATED_FILES:
                continue
            ext = _os.path.splitext(fname)[1].lower()
            if ext not in _GOVERNED_EXTENSIONS:
                continue

            abs_path = _os.path.join(dirpath, fname)
            rel_path = _os.path.relpath(abs_path, root).replace(_os.sep, "/")

            try:
                st = _os.stat(abs_path)
                manifest[rel_path] = {"mtime": st.st_mtime, "size": st.st_size}
            except (OSError, PermissionError):
                continue

    return manifest


# ─── Write-guard shim ────────────────────────────────────────────────────────
# Advisory monkey-patches that auto-register file writes to the session.
# NEVER raises or blocks — purely observational.

_GUARDED_ROOT: str | None = None
_GUARD_INSTALLED: bool = False


def reset_session_state() -> None:
    """Reset all session state to initial values.

    The MCP server calls this once when a server process boots. Calls within that
    process intentionally share state until ``anchor("new_session")`` rotates the logical
    session; this function does not provide per-client HTTP isolation.

    Clears:
        - File tracking sets (_SESSION_FILES_CHANGED, _SESSION_FILES_CREATED)
        - Timing log (_SESSION_TIMINGS)
        - Diff baselines (_SESSION_DIFF_BASELINES)
        - Session log (_SESSION_LOG)
        - Degraded component log (_SESSION_DEGRADED)
        - SessionState attributes (checkpoint count, learn debt, active spec, etc.)
        - AST cache

    Does NOT clear:
        - _SESSION_BOOT_MANIFEST (bootstrap-time filesystem snapshot, not session state)
        - Write guards (global install, not per-session)
    """
    global _SESSION_FILES_CHANGED, _SESSION_FILES_CREATED, _SESSION_TIMINGS
    global _SESSION_DIFF_BASELINES, _SESSION_LOG, _SESSION_DEGRADED, _SESSION_STATE

    # Clear mutable collections
    _SESSION_FILES_CHANGED.clear()
    _SESSION_FILES_CREATED.clear()
    _SESSION_TIMINGS.clear()
    _SESSION_DIFF_BASELINES.clear()
    _SESSION_PROVEN_CREATED.clear()
    _SESSION_PROVEN_BASELINES.clear()
    _SESSION_LOG.clear()
    _SESSION_DEGRADED.clear()

    # Reset SessionState fields to defaults
    _SESSION_STATE.files_at_last_checkpoint = 0
    _SESSION_STATE.prior_learn_debt = False
    _SESSION_STATE.checkpoint_in_progress = None
    _SESSION_STATE.boot_manifest = None
    _SESSION_STATE.notebook_path = None
    _SESSION_STATE.routing_stale = False
    reset_task_policy_state(_SESSION_STATE)
    _SESSION_STATE.repository_provider = None
    _SESSION_STATE.session_id = str(_uuid.uuid4())
    _SESSION_STATE.session_name = None
    _SESSION_STATE.skills_loaded.clear()

    # Clear AST cache
    ast_cache_clear()


def install_write_guard(root: str) -> None:
    """Monkey-patch builtins.open and pathlib.Path write methods to auto-register writes.

    This is advisory only — it NEVER blocks or raises. All guard logic is wrapped
    in try/except to prevent breaking normal file operations.

    Args:
        root: Absolute path to the project root to monitor.
    """
    global _GUARDED_ROOT, _GUARD_INSTALLED

    normalized_root = _os.path.abspath(root)
    if _GUARD_INSTALLED and normalized_root == _GUARDED_ROOT:
        return

    import builtins
    import pathlib

    # init() reloads Odibi Anchor modules. Remove wrappers from the prior
    # module before installing replacements so project switches never stack guards.
    uninstall_write_guard()
    _GUARDED_ROOT = normalized_root

    # Use module-level _GOVERNED_EXTENSIONS if available, otherwise define locally
    governed_extensions = globals().get(
        "_GOVERNED_EXTENSIONS",
        frozenset({
            ".py", ".sql", ".yml", ".yaml", ".json", ".toml", ".cfg", ".ini",
            ".md", ".txt", ".sh", ".bash", ".css", ".html", ".js", ".ts",
        }),
    )

    _ORIGINAL_OPEN = builtins.open

    def _prepare_write(file_path: str):
        """Capture pre-write provenance without mutating the session ledger."""
        if _GUARDED_ROOT is None:
            return None
        file_path = _os.path.abspath(file_path)
        try:
            common = _os.path.commonpath([file_path, _GUARDED_ROOT])
        except ValueError:
            return None
        if common != _GUARDED_ROOT:
            return None
        _, ext = _os.path.splitext(file_path)
        if ext.lower() not in governed_extensions:
            return None
        if _os.path.basename(file_path) in _CW_GENERATED_FILES:
            return None
        if is_managed_artifact_path(
            file_path, artifact_root=_SESSION_STATE.artifact_root,
            target_root=_SESSION_STATE.target_root or _GUARDED_ROOT,
        ):
            return None

        rel_path = _os.path.relpath(file_path, _GUARDED_ROOT).replace(_os.sep, "/")
        _ensure_boot_manifest_built()
        existed = _os.path.exists(file_path)
        boot_entry = _SESSION_BOOT_MANIFEST.get(rel_path)
        trusted_created = not existed and _BOOT_MANIFEST_BUILT and boot_entry is None
        trusted_baseline = False
        if existed and boot_entry is not None:
            try:
                current_stat = _os.stat(file_path)
                trusted_baseline = (
                    current_stat.st_mtime == boot_entry["mtime"] and
                    current_stat.st_size == boot_entry["size"]
                )
            except OSError:
                trusted_baseline = False
        baseline = None
        baseline_captured = False
        public_baseline_needed = (
            existed and rel_path not in _SESSION_DIFF_BASELINES and
            rel_path not in _SESSION_FILES_CREATED
        )
        private_baseline_needed = (
            existed and trusted_baseline and rel_path not in _SESSION_PROVEN_BASELINES
        )
        if public_baseline_needed or private_baseline_needed:
            try:
                with _ORIGINAL_OPEN(file_path, encoding="utf-8") as baseline_file:
                    baseline = baseline_file.read()
                baseline_captured = True
            except (OSError, UnicodeDecodeError, TypeError):
                pass
        return (
            rel_path, existed, baseline_captured, baseline,
            trusted_created, trusted_baseline, public_baseline_needed,
        )

    def _commit_write(prepared) -> None:
        if prepared is None:
            return
        (
            rel_path, existed, baseline_captured, baseline,
            trusted_created, trusted_baseline, public_baseline_needed,
        ) = prepared
        if not existed:
            if trusted_created and rel_path not in _SESSION_PROVEN_BASELINES:
                _SESSION_FILES_CREATED.add(rel_path)
                _SESSION_PROVEN_CREATED.add(rel_path)
                _SESSION_DIFF_BASELINES.pop(rel_path, None)
        elif baseline_captured:
            if public_baseline_needed and rel_path not in _SESSION_DIFF_BASELINES:
                _SESSION_DIFF_BASELINES[rel_path] = baseline
            if trusted_baseline:
                _SESSION_PROVEN_BASELINES[rel_path] = baseline
        _SESSION_FILES_CHANGED.add(rel_path)

    def _guarded_open(*args, **kwargs):
        prepared = None
        try:
            file_arg = args[0] if args else kwargs.get("file")
            mode = args[1] if len(args) > 1 else kwargs.get("mode", "r")
            if file_arg is not None and any(c in str(mode) for c in ("w", "a", "x", "+")):
                prepared = _prepare_write(str(file_arg))
        except Exception:
            prepared = None
        result = _ORIGINAL_OPEN(*args, **kwargs)
        try:
            _commit_write(prepared)
        except Exception:
            pass  # SILENT-OK: file-change tracking must never break writes
        return result

    _guarded_open._cw_original = _ORIGINAL_OPEN  # type: ignore[attr-defined]
    builtins.open = _guarded_open  # type: ignore[assignment]

    # --- pathlib.Path.write_text ---
    _original_write_text = pathlib.Path.write_text

    def _guarded_write_text(self, *args, **kwargs):
        try:
            prepared = _prepare_write(str(self))
        except Exception:
            prepared = None
        result = _original_write_text(self, *args, **kwargs)
        try:
            _commit_write(prepared)
        except Exception:
            pass  # SILENT-OK: file-change tracking must never break writes
        return result

    _guarded_write_text._cw_original = _original_write_text  # type: ignore[attr-defined]
    pathlib.Path.write_text = _guarded_write_text  # type: ignore[assignment]

    # --- pathlib.Path.write_bytes ---
    _original_write_bytes = pathlib.Path.write_bytes

    def _guarded_write_bytes(self, *args, **kwargs):
        try:
            prepared = _prepare_write(str(self))
        except Exception:
            prepared = None
        result = _original_write_bytes(self, *args, **kwargs)
        try:
            _commit_write(prepared)
        except Exception:
            pass  # SILENT-OK: file-change tracking must never break writes
        return result

    _guarded_write_bytes._cw_original = _original_write_bytes  # type: ignore[attr-defined]
    pathlib.Path.write_bytes = _guarded_write_bytes  # type: ignore[assignment]

    _GUARD_INSTALLED = True


def uninstall_write_guard() -> None:
    """Restore process write functions wrapped by the current or a reloaded module."""
    global _GUARDED_ROOT, _GUARD_INSTALLED

    import builtins
    import pathlib

    builtins.open = getattr(builtins.open, "_cw_original", builtins.open)
    pathlib.Path.write_text = getattr(pathlib.Path.write_text, "_cw_original", pathlib.Path.write_text)
    pathlib.Path.write_bytes = getattr(pathlib.Path.write_bytes, "_cw_original", pathlib.Path.write_bytes)
    _GUARDED_ROOT = None
    _GUARD_INSTALLED = False
