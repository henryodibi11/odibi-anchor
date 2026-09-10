"""_boot.py — Project root detection and path constants.

Extracted from agent_init.py Phase 1 (revamp spec).
Pure functions with zero session-state dependencies.
"""
import json as _json
import os as _os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from odibi_anchor._runtime_paths import RuntimePaths, resolve_resource_root, resolve_runtime_paths

_INSTALL_ROOT = str(resolve_resource_root())


# ─── Environment profiles (config-driven, auto-detected) ─────────────────────
def _load_config(project_root: str | None = None) -> dict:
    """Load target-only config, or use legacy cwd/install fallback without a target.

    Returns the full config dict, or empty dict if not found/unreadable.
    """
    if project_root:
        candidates = [_os.path.join(project_root, ".anchor_config.json")]
    else:
        candidates = [
            _os.path.join(_os.getcwd(), ".anchor_config.json"),
            _os.path.join(_INSTALL_ROOT, ".anchor_config.json"),
        ]

    for path in dict.fromkeys(candidates):
        if _os.path.isfile(path):
            try:
                with open(path) as f:
                    return _json.load(f)
            except (ValueError, OSError):
                continue
    return {}


def _detect_profile(
    config: dict,
    environment: Mapping[str, str] | None = None,
) -> dict:
    """Auto-detect the active environment profile.

    Priority:
    1. ANCHOR_PROFILE env var (explicit override)
    2. DATABRICKS_RUNTIME_VERSION env var → "databricks"
    3. CI env var → "ci"
    4. First "local" profile whose home dir exists on disk
    5. Fallback: empty dict (use defaults)
    """
    active_environment = _os.environ if environment is None else environment
    profiles = config.get("environment", {}).get("profiles", {})
    explicit = active_environment.get("ANCHOR_PROFILE")
    if not profiles:
        if explicit:
            raise ValueError("ANCHOR_PROFILE is set but no environment profiles are declared")
        return {}

    # 1. Explicit override
    if explicit:
        if explicit not in profiles:
            raise ValueError(
                f"Unknown ANCHOR_PROFILE {explicit!r}; expected one of {sorted(profiles)}"
            )
        return profiles[explicit]

    # 2. Databricks
    if active_environment.get("DATABRICKS_RUNTIME_VERSION") and "databricks" in profiles:
        return profiles["databricks"]

    # 3. CI
    if active_environment.get("CI") and "ci" in profiles:
        return profiles["ci"]

    # 4. Local — match by home path existence
    for profile in profiles.values():
        if profile.get("platform") == "local":
            home = profile.get("home")
            if home and _os.path.isdir(home):
                return profile

    # 5. Fallback
    return {}


def _resolve_environment(
    config: dict,
    environment: Mapping[str, str] | None = None,
) -> dict:
    """Resolve the active environment settings, defaulting null values to cwd.

    Returns a dict with keys: platform, home, framework_root, anchor_root,
    project_roots, sync_target, skills_dir, tools_dir, enforcement, memory_db,
    and runtime_paths.
    """
    active_environment = _os.environ if environment is None else environment
    profile = _detect_profile(config, active_environment)
    profiles = config.get("environment", {}).get("profiles", {})
    profile_name = next(
        (name for name, candidate in profiles.items() if candidate is profile),
        None,
    )
    cwd = _os.getcwd()
    runtime_paths = resolve_runtime_paths(
        profile.get("anchor_root"),
        environment=active_environment,
    )
    anchor_root = str(runtime_paths.anchor_home)

    def canonical_profile_path(value: str | None, fallback: str | None = None) -> str | None:
        selected = value or fallback
        return str(Path(selected).expanduser().resolve()) if selected else None

    # Memory DB: the shared SQLite knowledge store. Resolve per environment so
    # every agent/machine points at the same committed DB (ANCHOR_ROOT/.agent_memory.db)
    # instead of one hardcoded user path. ANCHOR_MEMORY_DB overrides (used by tests).
    memory_db = (
        active_environment.get("ANCHOR_MEMORY_DB")
        or profile.get("memory_db")
        or _os.path.join(anchor_root, ".agent_memory.db").replace("\\", "/")
    )
    return {
        "profile": profile_name,
        "platform": profile.get("platform", "local"),
        "home": canonical_profile_path(profile.get("home"), cwd),
        "framework_root": canonical_profile_path(profile.get("framework_root"), cwd),
        "anchor_root": anchor_root,
        "project_roots": [
            canonical_profile_path(root) for root in profile.get("project_roots") or []
        ],
        "sync_target": canonical_profile_path(profile.get("sync_target")),
        "skills_dir": canonical_profile_path(
            profile.get("skills_dir"), str(runtime_paths.skills_dir)
        ),
        "tools_dir": str(runtime_paths.tools_dir),
        "enforcement": profile.get("enforcement", "standard"),
        "memory_db": memory_db,
        "runtime_paths": runtime_paths,
    }


def resolve_boot_environment(
    project_root: str | None = None,
    *,
    environment: Mapping[str, str] | None = None,
) -> dict:
    """Resolve boot settings from the current config, profile, and environment."""
    return _resolve_environment(_load_config(project_root), environment)


@dataclass(frozen=True)
class InstalledProjectBootstrap:
    """Pure installed-orb path resolution before dispatcher initialization."""

    runtime_paths: RuntimePaths
    requested_target: str
    canonical_target: Path
    target_source: str
    runtime_kind: str
    memory_db: Path
    authority: tuple[tuple[str, Any], ...]


def _installed_boot_authority(environment: Mapping[str, Any]) -> tuple[tuple[str, Any], ...]:
    """Freeze every resolved field that can affect installed dispatcher authority."""
    keys = (
        "profile",
        "platform",
        "home",
        "framework_root",
        "anchor_root",
        "sync_target",
        "skills_dir",
        "tools_dir",
        "enforcement",
        "memory_db",
    )
    authority = [(key, environment.get(key)) for key in keys]
    authority.append(("project_roots", tuple(environment.get("project_roots") or ())))
    return tuple(authority)


def _validate_explicit_orb_value(
    name: str,
    environment: Mapping[str, str],
    *,
    path: bool,
) -> str | None:
    if name not in environment:
        return None
    value = environment[name]
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"{name} is set but empty")
    if "\n" in value or "\r" in value:
        raise RuntimeError(f"{name} must not contain line breaks")
    if path and (value.startswith("~") or not Path(value).is_absolute()):
        raise RuntimeError(f"{name} must be an absolute, non-tilde path")
    return value


def _paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def resolve_installed_project_bootstrap(
    target: str | _os.PathLike[str],
    environment: Mapping[str, str],
) -> InstalledProjectBootstrap:
    """Resolve one explicit checkout for an installed governed orb without writes."""
    if not isinstance(target, (str, _os.PathLike)) or isinstance(target, bytes):
        raise RuntimeError("ANCHOR_PROJECT_ROOT must be an absolute, non-tilde path")
    requested_target = _os.fspath(target)
    if not isinstance(requested_target, str) or not requested_target.strip():
        raise RuntimeError("ANCHOR_PROJECT_ROOT is required and must be non-empty")
    if "\n" in requested_target or "\r" in requested_target:
        raise RuntimeError("ANCHOR_PROJECT_ROOT must not contain line breaks")
    if requested_target.startswith("~") or not Path(requested_target).is_absolute():
        raise RuntimeError("ANCHOR_PROJECT_ROOT must be an absolute, non-tilde path")

    _validate_explicit_orb_value("ANCHOR_HOME", environment, path=True)
    _validate_explicit_orb_value("ANCHOR_MEMORY_DB", environment, path=True)
    explicit_profile = _validate_explicit_orb_value("ANCHOR_PROFILE", environment, path=False)
    configured_target = _validate_explicit_orb_value("ANCHOR_PROJECT_ROOT", environment, path=True)
    try:
        canonical_target = Path(requested_target).resolve(strict=True)
    except OSError as exc:
        raise RuntimeError(f"ANCHOR_PROJECT_ROOT does not resolve: {requested_target}") from exc
    if not canonical_target.is_dir():
        raise RuntimeError(f"ANCHOR_PROJECT_ROOT is not a directory: {canonical_target}")
    if configured_target is None:
        raise RuntimeError("ANCHOR_PROJECT_ROOT is required in portable installed-orb mode")
    try:
        configured_canonical = Path(configured_target).resolve(strict=True)
    except OSError as exc:
        raise RuntimeError(f"ANCHOR_PROJECT_ROOT does not resolve: {configured_target}") from exc
    if configured_canonical != canonical_target:
        raise RuntimeError("ANCHOR_PROJECT_ROOT does not match the requested target")

    config = _load_config(str(canonical_target))
    if explicit_profile is not None:
        profiles = config.get("environment", {}).get("profiles", {})
        if explicit_profile not in profiles:
            raise RuntimeError(f"ANCHOR_PROFILE is not declared by the target: {explicit_profile}")
    boot_environment = resolve_boot_environment(
        str(canonical_target),
        environment=environment,
    )
    runtime_paths = boot_environment["runtime_paths"]
    if runtime_paths.source_checkout:
        raise RuntimeError("portable installed-orb mode requires an installed distribution")
    resource_root = runtime_paths.resource_root
    anchor_home = runtime_paths.anchor_home
    memory_db = Path(boot_environment["memory_db"]).expanduser().resolve(strict=False)
    if _paths_overlap(canonical_target, resource_root):
        raise RuntimeError("ANCHOR_PROJECT_ROOT must be outside installed distribution resources")
    if anchor_home.exists() and not anchor_home.is_dir():
        raise RuntimeError("ANCHOR_HOME must resolve to a directory location")
    if memory_db == anchor_home or memory_db.is_dir():
        raise RuntimeError("ANCHOR_MEMORY_DB must resolve to a file location")
    if Path(boot_environment["skills_dir"]).resolve(strict=False) != runtime_paths.skills_dir:
        raise RuntimeError("an environment profile cannot relocate installed skills")
    for name, candidate in (("ANCHOR_HOME", anchor_home), ("ANCHOR_MEMORY_DB", memory_db)):
        if _paths_overlap(candidate, canonical_target):
            raise RuntimeError(f"{name} must be outside ANCHOR_PROJECT_ROOT")
        if _paths_overlap(candidate, resource_root):
            raise RuntimeError(f"{name} must be outside installed distribution resources")

    return InstalledProjectBootstrap(
        runtime_paths=runtime_paths,
        requested_target=requested_target,
        canonical_target=canonical_target,
        target_source="ANCHOR_PROJECT_ROOT",
        runtime_kind="installed_distribution",
        memory_db=memory_db,
        authority=_installed_boot_authority(boot_environment),
    )


# ─── Constants (config-driven; names/types stable, values per environment) ───
_ENV = resolve_boot_environment(_os.environ.get("_CW_BOOT_CONFIG_ROOT"))
_RUNTIME_PATHS = _ENV["runtime_paths"]

FRAMEWORK_ROOT = _ENV["framework_root"]
ANCHOR_ROOT = _ENV["anchor_root"]
_PROJECT_ROOTS = _ENV["project_roots"]
_USER_HOME = _ENV["home"]


def _resolve_skills_dir() -> str:
    """Return the single authoritative Odibi Anchor skills directory."""
    return _ENV["skills_dir"]


def _discover_project_roots(home_dir: str = None) -> list[str]:
    """Find all directories containing .anchor_manifest.json or .agent_memory.db.

    Scans common project parent directories (shallow — os.listdir only, no walk).
    Merges with the hardcoded _PROJECT_ROOTS for backward compatibility.

    Returns deduplicated list of project root paths.
    """
    if home_dir is None:
        home_dir = _USER_HOME
    roots = list(_PROJECT_ROOTS)
    normalized_home = home_dir.replace("\\", "/")
    if (
        _os.path.exists(_os.path.join(home_dir, ".anchor_manifest.json"))
        or _os.path.exists(_os.path.join(home_dir, ".agent_memory.db"))
    ) and normalized_home not in roots:
        roots.append(normalized_home)

    # Walk common locations for manifest/memory files
    candidate_parents = [
        _os.path.join(home_dir, "data-engineering"),
        _os.path.join(home_dir, "tools"),
        home_dir,
    ]

    for parent_dir in candidate_parents:
        if not _os.path.isdir(parent_dir):
            continue
        try:
            for entry in _os.listdir(parent_dir):
                full = _os.path.join(parent_dir, entry)
                if not _os.path.isdir(full):
                    continue
                # Normalize to forward slashes so comparisons against the
                # forward-slash config paths (ANCHOR_ROOT etc.) hold on Windows too.
                full = full.replace("\\", "/")
                if (_os.path.exists(_os.path.join(full, ".anchor_manifest.json")) or
                        _os.path.exists(_os.path.join(full, ".agent_memory.db"))):
                    if full not in roots:
                        roots.append(full)
        except (OSError, PermissionError):
            continue  # FUSE mount may be flaky

    return roots


def _detect_project_root(caller_globals=None):
    """Detect project root from notebook path, dbutils, or cwd.

    Priority:
    1. _AGENT_ROOT variable (if pre-set by caller before exec)
    2. dbutils notebook path (reliable in notebook cells)
    3. DATABRICKS_NOTEBOOK_PATH env var
    4. os.getcwd()
    5. Walk up looking for .agent_memory.db
    6. Fallback: user home

    Args:
        caller_globals: The globals() dict from the exec context (needed to
            check for _AGENT_ROOT). If None, skips priority 1.
    """
    # Priority 1: Explicit override (set before exec'ing this file)
    if caller_globals is not None:
        _override = caller_globals.get("_AGENT_ROOT", None)
        if _override:
            return _override

    nb_path = ""

    # Priority 2: dbutils (works inside notebook cells)
    try:
        import builtins
        _dbutils = getattr(builtins, "dbutils", None)
        # Try the caller's globals for dbutils.
        if _dbutils is None and caller_globals and "dbutils" in caller_globals:
            _dbutils = caller_globals["dbutils"]
        if _dbutils:
            _ctx = _dbutils.notebook.entry_point.getDbutils().notebook().getContext()
            nb_path = _ctx.notebookPath().get()
            # Normalize: add /Workspace prefix if missing
            if nb_path and not nb_path.startswith("/Workspace"):
                nb_path = f"/Workspace{nb_path}"
    except Exception:
        pass  # SILENT-OK: Databricks context detection — expected to fail outside notebooks

    # Priority 3: Env var
    if not nb_path:
        nb_path = _os.environ.get("DATABRICKS_NOTEBOOK_PATH", "")
        if nb_path and not nb_path.startswith("/Workspace"):
            nb_path = f"/Workspace{nb_path}" if nb_path.startswith("/Users") else nb_path

    # Priority 4: cwd
    if not nb_path:
        nb_path = _os.getcwd()

    # Match against known + discovered project roots (longest match wins)
    _all_roots = _discover_project_roots()
    matches = [r for r in _all_roots if nb_path.startswith(r)]
    if matches:
        return max(matches, key=len)  # longest prefix = most specific

    # Priority 5: Walk up from path looking for .agent_memory.db
    path = nb_path
    for _ in range(10):
        if _os.path.exists(f"{path}/.agent_memory.db"):
            return path
        parent = _os.path.dirname(path)
        if parent == path:
            break
        path = parent

    # Priority 6: User home
    return _USER_HOME


# ─── Boot memory ranking ─────────────────────────────────────────────────────
_BOOT_MEMORY_LIMIT = 8

_BOOT_TYPE_PRIORITY = {
    "gotcha": 0,
    "convention": 1,
    "pattern": 2,
    "decision": 3,
    "discovery": 4,
    "preference": 5,
    "failure_pattern": 6,
    "tool_call": 7,
}


def _rank_boot_entries(entries: list[dict], limit: int = 8) -> list[dict]:
    """Rank boot entries for maximum usefulness.

    Strategy: type-diverse selection weighted by explicit evidence and confidence.
    Ensures at least one entry per type (if available) before showing
    duplicates of the same type. Gotchas/conventions surface first.
    """
    if len(entries) <= limit:
        return sorted(entries, key=lambda e: _BOOT_TYPE_PRIORITY.get(e.get("type", ""), 99))

    # Score each entry: type priority (lower=better) + confidence boost + use_count boost
    for e in entries:
        e["_boot_score"] = (
            -_BOOT_TYPE_PRIORITY.get(e.get("type", ""), 99) * 10
            + e.get("confidence", 0.5) * 5
            + min(e.get("applied_count", 0), 5) * 1
            + min(e.get("confirmation_count", 0), 3) * 0.5
        )

    # Greedy type-diverse selection: pick best from each type first
    by_type: dict[str, list[dict]] = {}
    for e in entries:
        by_type.setdefault(e.get("type", "unknown"), []).append(e)

    # Sort within each type by score
    for entries_list in by_type.values():
        entries_list.sort(key=lambda e: e["_boot_score"], reverse=True)

    # Round-robin: 1 from each type (in priority order), then fill remaining
    selected = []
    type_order = sorted(by_type.keys(), key=lambda t: _BOOT_TYPE_PRIORITY.get(t, 99))

    for t in type_order:
        if by_type[t] and len(selected) < limit:
            selected.append(by_type[t].pop(0))

    # Fill remaining slots with highest-scored entries across all types
    remaining = [e for bucket in by_type.values() for e in bucket]
    remaining.sort(key=lambda e: e["_boot_score"], reverse=True)
    for e in remaining:
        if len(selected) >= limit:
            break
        selected.append(e)

    # Clean up temp key
    for e in selected:
        e.pop("_boot_score", None)
    for e in remaining:
        e.pop("_boot_score", None)

    return selected



# ─── Boot Sequence ────────────────────────────────────────────────────────────
# Extracted from agent_init.py Phase D of the Dispatcher Extraction spec.


@dataclass
class BootResult:
    """Result of the boot sequence — everything agent_init.py needs to set up."""
    project: str = "unknown"
    boot_entries: list = field(default_factory=list)
    mem_total: int = 0
    mem_count: int = 0
    promoted_count: int = 0
    manifest: dict = field(default_factory=dict)
    manifest_msg: str = "unavailable"
    env_manifest_msg: str = "not found"
    frame: Any = None  # ContextFrame or None
    prior_learn_debt: bool = False
    debt_info: dict = field(default_factory=dict)  # stage, files, timestamp
    task_rebind: dict | None = None
    learning_recovery_status: str = "not_checked"


def run_boot(
    root: str,
    anchor_root: str,
    *,
    state_root: str | None = None,
    project_id: str | None = None,
    db_path: str,
    boot_memory_limit: int = 8,
    frame_enabled: bool = True,
    recovered_learning_obligation: dict | None = None,
    learning_recovery_checked: bool = False,
    rebind_task: bool = False,
    route_binding: object | None = None,
) -> BootResult:
    """Execute the boot sequence and return initialized state.

    This encapsulates: project resolution, learn debt detection, manifest loading,
    context frame creation, and status banner printing.

    Args:
        root: Project root path.
        anchor_root: Context workbench root path.
        state_root: Optional durable root for cross-session continuity state.
        project_id: Stable logical project identity, when managed by the workspace.
        db_path: Path to the memory database.
        boot_memory_limit: Retained compatibility option; startup retrieval is deferred.
        frame_enabled: Whether to create a ContextFrame.
        rebind_task: Explicitly restore the newest matching open accepted task.

    Returns:
        BootResult with all boot state for agent_init.py to unpack.
    """
    import json as _json

    from odibi_anchor._dispatcher._session import (
        _load_continuity_state,
        _load_session_state,
        _save_continuity_state,
        _save_session_state,
    )
    from odibi_anchor._utils._session_state import _SESSION_STATE
    from odibi_anchor.codebase._memory_db import resolve_project as _db_resolve_project

    result = BootResult()
    # The dataclass default is only a prospective local identifier. It is not
    # accepted-task authority and must never qualify an owner-scoped lookup.
    _SESSION_STATE.task_window_id = None

    # ── Resolve memory-store identity without pre-task retrieval ─────────────
    try:
        result.project = project_id or _db_resolve_project(root)
    except Exception as _exc:
        result.project = "unknown"
        from odibi_anchor._utils._session_state import record_degraded
        record_degraded("boot_project_identity", _exc)

    # ── Restore an exact task owner before checking owner-scoped learn debt ──
    _continuity_legacy = None
    try:
        if route_binding is None:
            _prior_state = _load_session_state(state_root or root)
        else:
            continuity = _load_continuity_state(route_binding, _SESSION_STATE)
            _prior_state = continuity["state"]
            legacy = continuity.get("legacy_migration")
            if legacy and legacy.get("status") == "preserved_ambiguous":
                _continuity_legacy = legacy
                _save_continuity_state(
                    route_binding,
                    _SESSION_STATE,
                    {
                        "legacy_migration": {
                            "status": "preserved_ambiguous",
                            "source_sha256": legacy["source_sha256"],
                            "backup_path": legacy["backup_path"],
                        }
                    },
                )
                _prior_state = {}
        _SESSION_STATE.active_problem = _prior_state.get("active_problem")
        _SESSION_STATE.terminal_status = _prior_state.get("terminal_status")
        _SESSION_STATE.terminal_basis = _prior_state.get("terminal_basis")
        _SESSION_STATE.terminal_reason = _prior_state.get("terminal_reason")
        if route_binding is None and _prior_state.get("task_window_id"):
            _SESSION_STATE.task_window_id = _prior_state["task_window_id"]
            from odibi_anchor.codebase._memory_lifecycle import task_memory_activity

            memory_activity = task_memory_activity(
                db_path, task_window_id=_SESSION_STATE.task_window_id,
            )
            _SESSION_STATE.memory_selections = memory_activity["memory_selections"]
            _SESSION_STATE.memory_dispositions = memory_activity["memory_dispositions"]
            _SESSION_STATE.memory_applications = memory_activity["memory_applications"]
            _SESSION_STATE.memory_evaluations = memory_activity["memory_evaluations"]
        _SESSION_STATE.latest_closed_obligation_id = _prior_state.get(
            "latest_closed_obligation_id"
        )
    except Exception as _exc:
        if route_binding is not None:
            raise
        _prior_state = {}
        from odibi_anchor._utils._session_state import record_degraded
        record_degraded("boot_prior_session_state", _exc)

    if rebind_task:
        from odibi_anchor.codebase._task_authority import rebind_latest_open_task

        result.task_rebind = rebind_latest_open_task(db_path, session_state=_SESSION_STATE)
        if route_binding is not None:
            legacy_status = None
            if _continuity_legacy is not None:
                legacy_status = {
                    "status": (
                        "migrated_exact_owner"
                        if _continuity_legacy["state"].get("task_window_id")
                        == _SESSION_STATE.task_window_id
                        else "preserved_ambiguous"
                    ),
                    "source_sha256": _continuity_legacy["source_sha256"],
                    "backup_path": _continuity_legacy["backup_path"],
                }
            _save_continuity_state(
                route_binding,
                _SESSION_STATE,
                {
                    "stage": "rebound",
                    "task_window_id": _SESSION_STATE.task_window_id,
                    "active_problem": _SESSION_STATE.active_problem,
                    **({"legacy_migration": legacy_status} if legacy_status else {}),
                },
            )

    active_obligation = None
    owner_project = result.project if result.project != "unknown" else None
    owner_task = _SESSION_STATE.task_window_id
    if owner_project and owner_task:
        result.learning_recovery_status = "owner_checked"
        try:
            from odibi_anchor.codebase.structured_learning_context import (
                active_learning_obligation,
                initialize_learning_schema,
                latest_closed_learning_obligation,
            )
            try:
                active_obligation = active_learning_obligation(
                    project_id=owner_project, task_window_id=owner_task,
                )
            except RuntimeError as exc:
                if str(exc) != "learning schema drift":
                    raise
                initialize_learning_schema()
                active_obligation = active_learning_obligation(
                    project_id=owner_project, task_window_id=owner_task,
                )
            if active_obligation is not None:
                _SESSION_STATE.learning_obligation_id = active_obligation["obligation_id"]
                result.prior_learn_debt = True
                _SESSION_STATE.prior_learn_debt = True
                result.learning_recovery_status = "active_obligation"
                result.debt_info = {
                    "stage": "gated",
                    "files": len(_prior_state.get("files_changed", [])),
                    "timestamp": active_obligation["activated_at"],
                    "session_files": _prior_state.get("files_changed", []),
                    "closure_routes": ["structured assessment"],
                }
            else:
                latest = latest_closed_learning_obligation(
                    project_id=owner_project, task_window_id=owner_task,
                )
                _SESSION_STATE.learning_obligation_id = None
                _SESSION_STATE.prior_learn_debt = False
                if latest is not None:
                    _SESSION_STATE.latest_closed_obligation_id = latest["obligation_id"]
                    result.learning_recovery_status = "latest_closed_obligation"
                    corrected = dict(_prior_state)
                    corrected.update({
                        "awaiting_learn": False,
                        "learning_obligation_id": None,
                        "latest_closed_obligation_id": latest["obligation_id"],
                    })
                    if route_binding is None:
                        _save_session_state(state_root or root, corrected, strict=True)
                    else:
                        _save_continuity_state(route_binding, _SESSION_STATE, corrected)
                elif _prior_state.get("awaiting_learn"):
                    result.prior_learn_debt = True
                    _SESSION_STATE.prior_learn_debt = True
                    result.learning_recovery_status = "legacy_session_debt"
                    result.debt_info = {
                        "stage": _prior_state.get("stage", "gated"),
                        "files": len(_prior_state.get("files_changed", [])),
                        "timestamp": _prior_state.get("timestamp", "unknown"),
                        "session_files": _prior_state.get("files_changed", []),
                        "closure_routes": ["legacy learn"],
                    }
        except Exception as _exc:
            result.learning_recovery_status = "owner_lookup_failed"
            from odibi_anchor._utils._session_state import record_degraded
            record_degraded("boot_prior_session_state", _exc)
    else:
        result.learning_recovery_status = "skipped_no_exact_owner"
        if _prior_state.get("awaiting_learn"):
            # Legacy unstructured debt is state-local and does not require a
            # project-global ledger lookup.
            result.prior_learn_debt = True
            _SESSION_STATE.prior_learn_debt = True
            result.debt_info = {
                "stage": _prior_state.get("stage", "gated"),
                "files": len(_prior_state.get("files_changed", [])),
                "timestamp": _prior_state.get("timestamp", "unknown"),
                "session_files": _prior_state.get("files_changed", []),
                "closure_routes": ["legacy learn"],
            }

    if rebind_task and active_obligation is not None:
        raise RuntimeError("accepted task cannot be rebound while learning recovery is active")

    # Compatibility parameters are intentionally ignored. Recovery must be
    # derived from the exact route and task owner, never a process-global row.
    del recovered_learning_obligation, learning_recovery_checked

    # ── Environment manifest ──────────────────────────────────────────────────
    try:
        _env_manifest_path = _os.path.join(anchor_root, "env_manifests", f"{result.project}.json")
        if _os.path.exists(_env_manifest_path):
            with open(_env_manifest_path, encoding="utf-8") as _f:
                result.manifest = _json.load(_f)
            result.manifest_msg = "0 files"
            result.env_manifest_msg = f"loaded ({result.project})"
        else:
            result.env_manifest_msg = f"not found ({result.project})"
    except Exception as _exc:
        result.env_manifest_msg = "error loading"  # non-blocking
        from odibi_anchor._utils._session_state import record_degraded
        record_degraded("boot_env_manifest", _exc)

    # ── Project manifest (.anchor_manifest.json) fallback ────────────────────────
    # When no env_manifest exists, load the project's own .anchor_manifest.json so
    # the banner shows 'loaded' instead of 'unavailable'.
    if not result.manifest:
        try:
            from odibi_anchor.codebase._manifest import load_manifest as _load_manifest
            _project_manifest = _load_manifest(root)
            if _project_manifest:
                result.manifest = _project_manifest
                _errors = _project_manifest.get("_validation_errors", [])
                result.manifest_msg = (
                    f"loaded ({len(_errors)} warnings)" if _errors else "loaded"
                )
        except Exception:
            pass  # SILENT-OK: project manifest loading is best-effort

    # ── Context Frame init ────────────────────────────────────────────────────
    if frame_enabled:
        try:
            from odibi_anchor._utils._context_frame import ContextFrame as _ContextFrame
            result.frame = _ContextFrame(
                session_id=_SESSION_STATE.session_id,
                project=result.project,
                manifest=result.manifest if result.manifest else None,
            )
            if result.task_rebind is not None:
                result.frame.record("task", {
                    "kind": "task_execution_context",
                    "subject": _SESSION_STATE.task_goal or "",
                    "mode": _SESSION_STATE.active_task_mode or "",
                    "verification": {
                        "acceptance_criteria": result.task_rebind["obligations"][
                            "acceptance_criteria"
                        ],
                    },
                })
        except Exception as _exc:
            from odibi_anchor._utils._session_state import record_degraded
            record_degraded("boot_frame_init", _exc)

    # ── Write guard & boot manifest ──────────────────────────────────────────
    from odibi_anchor._utils._session_state import (
        build_boot_manifest as _build_boot_manifest,
    )
    from odibi_anchor._utils._session_state import (
        install_write_guard as _install_write_guard,
    )
    try:
        _build_boot_manifest(root)
        _write_guard_active = True
    except Exception:
        _write_guard_active = False  # SILENT-OK: manifest build is best-effort
    try:
        _install_write_guard(root)
    except Exception:
        _write_guard_active = False  # SILENT-OK: write guard is best-effort

    # ── Status banner ─────────────────────────────────────────────────────────
    print(f"[Anchor] Ready | Root: {root} | Project: {result.project}")
    print("[Anchor] Memory: bounded retrieval deferred to task acceptance")
    print(f"[Anchor] Manifest: {result.manifest_msg} | Write guard: {'active' if _write_guard_active else 'inactive'}")
    print(f"[Anchor] Env Manifest: {result.env_manifest_msg}")
    if result.prior_learn_debt:
        _di = result.debt_info
        print(f"[Anchor] ** ABANDONED SESSION DETECTED (stage: {_di['stage']}, {_di['files']} files, at {_di['timestamp']})")
        if _di.get("session_files"):
            print(f"[Anchor]    Files from previous session: {', '.join(_di['session_files'][:5])}")
        if _di.get("closure_routes") == ["legacy learn"]:
            print('[Anchor]    BLOCKED: historical debt requires compatibility anchor("learn") recovery.')
        else:
            print('[Anchor]    BLOCKED: anchor("task") locked until structured assessment closes it.')
    print("[Anchor] Dispatch: anchor('action', ...) — 49 actions")
    print("[Anchor]")
    print(f"[Anchor] {'='*55}")
    print('[Anchor] ** MANDATORY SEQUENCE (enforced with RuntimeError):')
    from odibi_anchor._dispatcher._protocol import (
        PRE_DELIVERY_SEQUENCE,
        STARTUP_SEQUENCE,
        protocol_invocation,
    )
    for i, step in enumerate(STARTUP_SEQUENCE, 1):
        print(f'[Anchor]   {i}. {protocol_invocation(step)}')
    print('[Anchor]   ... edit files ...')
    for i, step in enumerate(PRE_DELIVERY_SEQUENCE, len(STARTUP_SEQUENCE) + 1):
        print(f'[Anchor]   {i}. {protocol_invocation(step)}')
    print(f'[Anchor]   Repeat {len(STARTUP_SEQUENCE)}-{len(STARTUP_SEQUENCE) + len(PRE_DELIVERY_SEQUENCE)} for each feature.')
    print(f"[Anchor] {'='*55}")

    return result
