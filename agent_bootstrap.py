"""Bootstrap this Odibi Anchor checkout in the caller's Python process.

Run with ``runpy.run_path(<exact-checkout>/agent_bootstrap.py)`` and retain the
returned namespace. Exceptions are bootstrap failures; locating this file alone
is not success.
"""

from __future__ import annotations

import importlib
import os
import sys
import uuid
from collections.abc import Mapping
from pathlib import Path


def _environment_path(name: str, default: Path) -> tuple[str, Path, bool]:
    if name in os.environ:
        raw = os.environ[name]
        if not raw.strip():
            raise RuntimeError(f"{name} is set but empty; provide a safe external path or unset it")
        if raw.startswith("~") or not Path(raw).is_absolute():
            raise RuntimeError(f"{name} must be an absolute, non-tilde path")
        return raw, Path(raw).resolve(), True
    resolved = default.resolve()
    value = str(resolved)
    return value, resolved, False


def _overlaps(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _module_origin(module: object) -> Path | None:
    origin = getattr(module, "__file__", None)
    return Path(origin).resolve() if origin else None


def _prioritize_source(source: Path) -> None:
    sys.path[:] = [
        entry
        for entry in sys.path
        if not isinstance(entry, str) or Path(entry or ".").resolve() != source
    ]
    sys.path.insert(0, str(source))


_REPOSITORY_CAPABILITIES = (
    "host_repository_identity",
    "local_worktree_status",
    "git_changed_paths_and_diff",
    "merge_base_and_history",
    "task_scoped_content_diff",
    "task_scoped_write_tracking",
    "pr_readiness",
)


def _unavailable_repository_evidence(reason: str) -> dict[str, object]:
    return {
        "kind": "unavailable",
        "provider_id": None,
        "acquisition_outcome": "unavailable",
        "reason": reason,
        "capabilities": {name: "unavailable" for name in _REPOSITORY_CAPABILITIES},
    }


def _local_git_repository_evidence(target: Path) -> dict[str, object]:
    repository_snapshot = importlib.import_module("odibi_anchor._repository_snapshot")
    if not repository_snapshot.canonical_local_git_available(target):
        return _unavailable_repository_evidence("target_is_not_a_canonical_local_git_worktree")
    return {
        "kind": "local_git",
        "provider_id": "odibi-anchor.local-git",
        "acquisition_outcome": "available",
        "reason": None,
        "capabilities": {name: "available" for name in _REPOSITORY_CAPABILITIES},
    }


def _explicit_repository_evidence(provider: object) -> dict[str, object]:
    return {
        "kind": "databricks_git_folder",
        "provider_id": getattr(provider, "provider_id", None),
        "acquisition_outcome": "deferred",
        "reason": "caller_supplied_provider_not_attested_during_bootstrap",
        "capabilities": {name: "unavailable" for name in _REPOSITORY_CAPABILITIES},
    }


ANCHOR_REPO = Path(__file__).resolve().parent
ANCHOR_SRC = (ANCHOR_REPO / "src").resolve()
EXPECTED_BOOTSTRAP = ANCHOR_SRC / "odibi_anchor" / "bootstrap.py"
if not EXPECTED_BOOTSTRAP.is_file():
    raise RuntimeError(f"Not a Odibi Anchor source checkout: missing {EXPECTED_BOOTSTRAP}")

_expected_package = ANCHOR_SRC / "odibi_anchor"
for _name, _module in tuple(sys.modules.items()):
    if _name == "odibi_anchor" or _name.startswith("odibi_anchor."):
        _origin = _module_origin(_module)
        if _origin is None or not (_origin == _expected_package or _expected_package in _origin.parents):
            raise RuntimeError(
                "A different Odibi Anchor is already loaded in this Python process; "
                "restart it before bootstrapping the selected checkout"
            )

_explicit_cw_home_value, _explicit_cw_home, _cw_home_explicit = _environment_path(
    "ANCHOR_HOME", ANCHOR_REPO
)
_explicit_memory_value, _explicit_memory, _memory_db_explicit = _environment_path(
    "ANCHOR_MEMORY_DB", ANCHOR_REPO / ".agent_memory.db"
)
if _cw_home_explicit and _overlaps(ANCHOR_REPO, _explicit_cw_home):
    raise RuntimeError("ANCHOR_HOME must be outside the Odibi Anchor source checkout")
if _memory_db_explicit and (
    _explicit_memory == ANCHOR_REPO or ANCHOR_REPO in _explicit_memory.parents
):
    raise RuntimeError("ANCHOR_MEMORY_DB must be outside the Odibi Anchor source checkout")
sys.dont_write_bytecode = True
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

_prioritize_source(ANCHOR_SRC)

_bootstrap = importlib.import_module("odibi_anchor.bootstrap")
_boot = importlib.import_module("odibi_anchor._dispatcher._boot")

# Consume the dispatcher's profile-aware resolution exactly as direct init and the CLI
# do. In particular, do not inject source-checkout defaults into the environment: that
# would outrank the selected profile when the dispatcher initializes.
_boot_environment = _boot.resolve_boot_environment()
ANCHOR_HOME = _boot_environment["runtime_paths"].anchor_home
_memory_db_value = _boot_environment["memory_db"]
ANCHOR_MEMORY_DB = Path(_memory_db_value).expanduser().resolve()

if Path(_bootstrap.__file__).resolve() != EXPECTED_BOOTSTRAP:
    raise RuntimeError(f"Imported bootstrap from unexpected source: {_bootstrap.__file__}")

ANCHOR_HOME.mkdir(parents=True, exist_ok=True)
_project = importlib.import_module("odibi_anchor._dispatcher._project")
_explicit_project = os.environ.get("ANCHOR_PROJECT_ID")
_route_binding = None
if _explicit_project is not None:
    _route_binding = _project.resolve_route_binding(
        ANCHOR_HOME,
        project=_explicit_project,
        runtime_instance_id=os.environ.get("ANCHOR_RUNTIME_INSTANCE_ID") or f"agent:{uuid.uuid4()}",
    )
else:
    try:
        _route_binding = _project.resolve_route_binding(
            ANCHOR_HOME,
            target_hint=ANCHOR_REPO,
            runtime_instance_id=os.environ.get("ANCHOR_RUNTIME_INSTANCE_ID") or f"agent:{uuid.uuid4()}",
        )
    except FileNotFoundError:
        # A source checkout with no matching registry entry retains the
        # documented interactive selector/external-root compatibility path.
        _route_binding = None
_active_project = (
    None
    if _route_binding is not None
    else _project.resolve_active_project(ANCHOR_HOME, None)
)
_requested_target = None if (_route_binding is not None or _active_project) else str(ANCHOR_REPO)
_target_hint = (
    Path(_route_binding.target_root).resolve(strict=False)
    if _route_binding is not None
    else (
        Path(_active_project["target_root"]).resolve(strict=False)
        if _active_project is not None
        else ANCHOR_REPO
    )
)
_repository_provider = globals().get("ANCHOR_REPOSITORY_PROVIDER")
_auto_repository_target = None
if _repository_provider is not None:
    REPOSITORY_EVIDENCE = _explicit_repository_evidence(_repository_provider)
else:
    REPOSITORY_EVIDENCE = _local_git_repository_evidence(_target_hint)
    if (
        REPOSITORY_EVIDENCE["kind"] == "unavailable"
        and _target_hint.as_posix().startswith("/Workspace/")
    ):
        _auto_repository_target = _target_hint
        _databricks = importlib.import_module("odibi_anchor.operational._databricks")
        _repository_provider, REPOSITORY_EVIDENCE = (
            _databricks.autoconfigure_databricks_git_folder_repository(_target_hint)
        )

_init_kwargs = {"root": _requested_target, "output_format": "dict"}
if _route_binding is not None:
    _init_kwargs["route_binding"] = _route_binding
if _repository_provider is not None:
    _init_kwargs["repository_provider"] = _repository_provider
anchor, ROOT, MANIFEST = _bootstrap.init(**_init_kwargs)
if (
    _auto_repository_target is not None
    and Path(ROOT).resolve(strict=False) != _auto_repository_target
):
    REPOSITORY_EVIDENCE = _unavailable_repository_evidence(
        "effective_target_differs_from_attested_databricks_checkout"
    )
_prioritize_source(ANCHOR_SRC)
ORIENTATION = anchor("orient", output_format="dict")
if not isinstance(ORIENTATION, Mapping) or ORIENTATION.get("kind") != "orientation":
    raise RuntimeError("Odibi Anchor orientation returned an invalid structured result")
for _part_name in ("status", "memory", "audit_history"):
    _part = ORIENTATION.get(_part_name)
    if not isinstance(_part, Mapping) or "error" in _part:
        raise RuntimeError(f"Odibi Anchor orientation failed for {_part_name}: {_part!r}")

BOOTSTRAP = {
    "success": True,
    "kind": "agent_bootstrap",
    "repository": str(ANCHOR_REPO),
    "state_home": str(ANCHOR_HOME),
    "memory_db": _memory_db_value,
    "requested_target": _requested_target,
    "effective_root": str(ROOT),
    "orientation_kind": ORIENTATION["kind"],
    "repository_evidence": REPOSITORY_EVIDENCE,
}
