"""Explicit, installed-distribution startup and read-only diagnostics."""
from __future__ import annotations

import hashlib
import importlib.metadata
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from odibi_anchor import __version__

_DATABRICKS_SDK_MINIMUM = "0.138.0"


def _version_tuple(value: str) -> tuple[int, ...]:
    """Return a conservative numeric prefix for capability qualification."""
    match = re.fullmatch(r"(\d+(?:\.\d+)*)", value)
    if not match:
        return ()
    parts = tuple(int(part) for part in match.group(1).split("."))
    return (*parts, 0, 0, 0)[:3]


def _databricks_capability(*, required: bool) -> dict[str, Any]:
    try:
        installed = importlib.metadata.version("databricks-sdk")
    except importlib.metadata.PackageNotFoundError:
        installed = None
    qualified = bool(
        installed
        and _version_tuple(installed) >= _version_tuple(_DATABRICKS_SDK_MINIMUM)
    )
    status = "ready" if qualified else ("missing" if installed is None else "outdated")
    if not required:
        status = "available" if qualified else "not_required"
    return {
        "status": status,
        "required": required,
        "minimum_version": _DATABRICKS_SDK_MINIMUM,
        "installed_version": installed,
        "qualified": qualified,
        "install_command": (
            None
            if qualified or not required
            else f'%pip install "odibi-anchor[databricks]=={__version__}"'
        ),
        "restart_required_after_install": required and not qualified,
    }


def _absolute_directory(value: str | os.PathLike[str], name: str) -> Path:
    text = os.fspath(value)
    if not text or "\n" in text or "\r" in text or text.startswith("~"):
        raise ValueError(f"{name} must be an absolute, single-line directory path")
    path = Path(text)
    if not path.is_absolute() or not path.is_dir():
        raise ValueError(f"{name} must be an existing absolute directory")
    return path.resolve()


def launch(
    *,
    anchor_home: str | os.PathLike[str],
    project_id: str | None = None,
    project_root: str | os.PathLike[str],
    output_format: str = "dict",
) -> Callable[..., Any]:
    """Bind one exact route and return a callable Anchor dispatcher.

    This entry point imports only package modules and therefore works from a wheel;
    it never searches for or adds a source checkout.
    """
    home = _absolute_directory(anchor_home, "ANCHOR_HOME")
    target = _absolute_directory(project_root, "ANCHOR_PROJECT_ROOT")
    environment_project = os.environ.get("ANCHOR_PROJECT_ID")
    if project_id is not None and (
        not isinstance(project_id, str) or not project_id.strip() or project_id != project_id.strip()
    ):
        raise ValueError("ANCHOR_PROJECT_ID must be a non-empty project ID")
    if project_id is not None and environment_project not in (None, project_id):
        raise RuntimeError("ANCHOR_PROJECT_ID conflicts with the requested startup route")
    requested_project = project_id or environment_project

    from odibi_anchor._dispatcher._project import resolve_route_binding

    route = resolve_route_binding(
        home, project=requested_project, target_hint=target,
        runtime_instance_id=f"startup:{os.getpid()}",
    )
    if route is None:
        raise RuntimeError("startup route resolution returned no binding")

    bindings = {
        "ANCHOR_HOME": str(home),
        "ANCHOR_PROJECT_ID": route.project_id,
        "ANCHOR_PROJECT_ROOT": route.target_root,
    }
    previous = {name: os.environ.get(name) for name in bindings}
    for name, value in bindings.items():
        current = os.environ.get(name)
        if current is not None and current != value:
            raise RuntimeError(f"{name} conflicts with the requested startup route")
    os.environ.update(bindings)

    from odibi_anchor.bootstrap import init

    try:
        anchor, _root, _manifest = init(route_binding=route, output_format=output_format)
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
    if not callable(anchor):
        raise RuntimeError("bootstrap did not return a callable Anchor dispatcher")
    return anchor


def register_project(
    *,
    anchor_home: str | os.PathLike[str],
    project_id: str,
    project_root: str | os.PathLike[str],
) -> dict[str, Any]:
    """Create one explicit managed-project route before first launch."""
    text = os.fspath(anchor_home)
    home = Path(text)
    if not text or "\n" in text or "\r" in text or text.startswith("~") or not home.is_absolute():
        raise ValueError("ANCHOR_HOME must be an explicit absolute, single-line path")
    home = home.resolve()
    if home.exists() and not home.is_dir():
        raise ValueError("ANCHOR_HOME must be a directory")
    target = _absolute_directory(project_root, "ANCHOR_PROJECT_ROOT")
    from odibi_anchor._dispatcher._project import _normalize_project_id, project_action

    if not isinstance(project_id, str) or _normalize_project_id(project_id) != project_id:
        raise ValueError("project_id must already be a normalized non-empty project ID")
    home.mkdir(parents=True, exist_ok=True)
    result = project_action(
        home, "create", name=project_id, target=target, output_format="dict"
    )
    assert isinstance(result, dict)
    return {
        "kind": "startup_project_registration",
        "status": "created",
        "project_id": result["project_id"],
        "anchor_home": str(home),
        "artifact_root": result["artifact_root"],
        "target_root": result["target_root"],
        "next_operation": {
            "operation": "launch",
            "arguments": {
                "anchor_home": str(home),
                "project_id": result["project_id"],
                "project_root": result["target_root"],
            },
        },
    }


def prepare_portfolio_runtime(
    *, config_path: str | os.PathLike[str], host_id: str, project_id: str,
    persona_id: str | None = None,
) -> dict[str, Any]:
    """Prepare one exact configured route and restore absent local state when available.

    This operation never selects an ambient project and never mutates process
    environment. It creates only the configured local state directory and missing
    managed-project registration needed by the returned immutable binding.
    """
    from odibi_anchor.portfolio import load_portfolio_document, resolve_project

    document = load_portfolio_document(config_path)
    resolved = resolve_project(
        document["portfolio"], host_id=host_id, project_id=project_id,
        persona_id=persona_id,
    )
    environment = resolved["environment"]
    home = Path(environment["ANCHOR_HOME"])
    target = _absolute_directory(environment["ANCHOR_PROJECT_ROOT"], "ANCHOR_PROJECT_ROOT")
    if home.exists() and not home.is_dir():
        raise ValueError("configured local_state_root must be a directory")

    database = Path(environment["ANCHOR_MEMORY_DB"])
    authority_id = environment["ANCHOR_AUTHORITY_ID"]
    trust_domain = environment["ANCHOR_TRUST_DOMAIN"]
    adapter = document["portfolio"]["hosts"][host_id]["adapter"]
    durable_root = environment.get("ANCHOR_DURABLE_ROOT")
    is_databricks = adapter == "databricks"
    if durable_root is not None and not is_databricks and not Path(durable_root).is_dir():
        raise FileNotFoundError(
            "configured durable_root is unavailable; refusing to initialize or reuse local state"
        )
    if durable_root is not None:
        from odibi_anchor.durability import qualify_durability

        qualify_durability(
            source_db=database,
            durable_root=durable_root,
            authority_id=authority_id,
            databricks=is_databricks,
        )
    home.mkdir(parents=True, exist_ok=True)
    restore: dict[str, Any] = {"status": "not_applicable", "reason": "local database already exists"}
    if not database.exists():
        snapshots: list[dict[str, Any]] = []
        if durable_root is not None:
            from odibi_anchor.durability import list_snapshots, restore_latest

            try:
                snapshots = list_snapshots(
                    durable_root=durable_root,
                    authority_id=authority_id,
                    databricks=is_databricks,
                )["snapshots"]
            except FileNotFoundError:
                snapshots = []
        if snapshots:
            assert durable_root is not None
            restore = restore_latest(
                durable_root=durable_root,
                destination_db=database,
                authority_id=authority_id,
                databricks=is_databricks,
            )
        else:
            restore = {"status": "not_applicable", "reason": "no durable snapshot exists"}
    from odibi_anchor.durability import ensure_database_authority

    ownership = ensure_database_authority(
        database, authority_id=authority_id, trust_domain=trust_domain,
        initialize=not database.exists(),
    )

    from odibi_anchor._dispatcher._project import resolve_route_binding

    registration: dict[str, Any]
    try:
        route = resolve_route_binding(
            home,
            project=project_id,
            target_hint=target,
            runtime_instance_id="portfolio:prepare",
        )
        assert route is not None
        registration = {"status": "existing", "project_id": route.project_id}
    except FileNotFoundError:
        registration = register_project(
            anchor_home=home, project_id=project_id, project_root=target
        )
        route = resolve_route_binding(
            home,
            project=project_id,
            target_hint=target,
            runtime_instance_id="portfolio:prepare",
        )
    assert route is not None
    return {
        "kind": "portfolio_runtime_preparation",
        "status": "ready",
        "config_path": document["path"],
        "config_sha256": document["sha256"],
        "host_id": host_id,
        "project_id": route.project_id,
        "target_root": route.target_root,
        "environment": environment,
        "persona": resolved["persona"],
        "registration": registration,
        "restore": restore,
        "authority": ownership,
        "next_operation": {
            "operation": "bootstrap",
            "arguments": {
                "script": str(
                    Path(document["portfolio"]["hosts"][host_id].get("instruction_root") or target)
                    / ".assistant"
                    / "agent_bootstrap.py"
                ),
                "environment": environment,
            },
        },
    }


def _open_tasks(database: Path, project_id: str | None, target: Path | None) -> dict[str, Any]:
    if not database.is_file():
        return {"status": "unavailable", "count": None, "implication": "no state database exists"}
    try:
        connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            tables = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
            if not {"accepted_task_records", "accepted_task_events"}.issubset(tables):
                return {"status": "unavailable", "count": None,
                        "implication": "accepted-task authority is not initialized"}
            if target is None:
                count = connection.execute(
                    "SELECT count(*) FROM accepted_task_records r WHERE NOT EXISTS "
                    "(SELECT 1 FROM accepted_task_events e WHERE e.task_window_id=r.task_window_id "
                    "AND e.event_type='closed')"
                ).fetchone()[0]
                implication = "route is unresolved; open tasks cannot be attributed to this startup"
            else:
                count = connection.execute(
                    "SELECT count(*) FROM accepted_task_records r WHERE r.project_id IS ? "
                    "AND r.target_root=? AND NOT EXISTS (SELECT 1 FROM accepted_task_events e "
                    "WHERE e.task_window_id=r.task_window_id AND e.event_type='closed')",
                    (project_id, str(target)),
                ).fetchone()[0]
                implication = (
                    "task_rebind is available" if count == 1 else
                    "no matching task can be rebound" if count == 0 else
                    "multiple matching tasks require explicit resolution"
                )
            return {"status": "observed", "count": count, "implication": implication}
        finally:
            connection.close()
    except (OSError, sqlite3.Error):
        return {"status": "unavailable", "count": None,
                "implication": "database could not be inspected read-only"}


def doctor(*, environment: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Return secret-free startup facts without creating or changing state."""
    env = os.environ if environment is None else environment
    from odibi_anchor._dispatcher._project import resolve_route_binding, route_binding_diagnostics
    from odibi_anchor._runtime_paths import resolve_runtime_paths

    paths = resolve_runtime_paths(environment=env)
    raw_project = env.get("ANCHOR_PROJECT_ID")
    raw_target = env.get("ANCHOR_PROJECT_ROOT")
    target = None
    route = None
    route_error = None
    try:
        if raw_target:
            target = _absolute_directory(raw_target, "ANCHOR_PROJECT_ROOT")
        route = resolve_route_binding(
            paths.anchor_home, project=raw_project, target_hint=target,
            runtime_instance_id="doctor:read-only",
        )
    except (OSError, RuntimeError, ValueError) as exc:
        route_error = str(exc)

    database = Path(env.get("ANCHOR_MEMORY_DB") or paths.anchor_home / ".agent_memory.db").resolve()
    route_inputs = {
        "ANCHOR_HOME": env.get("ANCHOR_HOME"),
        "ANCHOR_PROJECT_ID": raw_project,
        "ANCHOR_PROJECT_ROOT": raw_target,
    }
    is_databricks = bool(env.get("DATABRICKS_RUNTIME_VERSION"))
    databricks_capability = _databricks_capability(required=is_databricks)
    route_operation = (
        {
            "operation": "launch",
            "arguments": {
                "anchor_home": str(paths.anchor_home),
                "project_id": route.project_id,
                "project_root": route.target_root,
            },
        }
        if route is not None
        else {
            "operation": (
                "register_project"
                if raw_project and target and not (
                    paths.anchor_home / "workspace" / "projects" / raw_project / "PROJECT.md"
                ).is_file()
                else "resolve_route_inputs"
            ),
            "arguments": {
                "anchor_home": str(paths.anchor_home),
                **({"project_id": raw_project} if raw_project else {}),
                **({"project_root": str(target)} if target else {}),
            },
        }
    )
    next_operation = (
        {
            "operation": "install_dependency",
            "command": databricks_capability["install_command"],
            "restart_python": True,
            "reason": "Databricks durability requires the qualified Workspace Files API SDK.",
        }
        if is_databricks and not databricks_capability["qualified"]
        else route_operation
    )
    return {
        "kind": "startup_doctor",
        "read_only": True,
        "package": {"name": "odibi-anchor", "version": __version__},
        "home": {"path": str(paths.anchor_home), "exists": paths.anchor_home.is_dir()},
        "database": {"path": str(database), "exists": database.is_file()},
        "host": {"platform": sys.platform, "databricks": is_databricks},
        "capabilities": {
            "databricks_sdk": databricks_capability,
        },
        "filesystem": {"resource_root": str(paths.resource_root),
                       "source_checkout": paths.source_checkout,
                       "concurrency_capability": "not_qualified"},
        "route_inputs": route_inputs,
        "routing": {"status": "exact" if route else "ambiguous_or_invalid",
                    "diagnostics": route_binding_diagnostics(route) if route else None,
                    "reason": route_error},
        "next_operation": next_operation,
        "tasks": _open_tasks(database, raw_project, target),
        "concurrency": {
            "status": "unqualified",
            "implication": "doctor performs no multi-process or filesystem-locking probe; "
                           "single-writer safety must not be inferred",
        },
    }


def install_guidance(target_root: str | os.PathLike[str]) -> dict[str, Any]:
    """Copy the packaged agent contract into one explicit repository.

    Existing guidance is never overwritten. Updating an installed contract requires
    an explicit human-reviewed replacement rather than a silent package-side mutation.
    """
    target = _absolute_directory(target_root, "target_root")
    from odibi_anchor._runtime_paths import resolve_resource_root

    resources = resolve_resource_root()
    sources = {
        ".assistant": resources / ".assistant",
        ".assistant_instructions.md": resources / ".assistant_instructions.md",
    }
    missing = [name for name, path in sources.items() if not path.exists()]
    if missing:
        raise RuntimeError("installed distribution is missing guidance resources: " + ", ".join(missing))
    collisions = [name for name in sources if (target / name).exists()]
    if collisions:
        raise RuntimeError("guidance destination collision: " + ", ".join(collisions))

    digest = hashlib.sha256()
    file_count = 0
    for _name, source in sources.items():
        candidates = source.rglob("*") if source.is_dir() else (source,)
        for candidate in sorted(path for path in candidates if path.is_file()):
            relative = candidate.relative_to(resources).as_posix()
            digest.update(relative.encode() + b"\0" + candidate.read_bytes() + b"\0")
            file_count += 1

    staging = Path(tempfile.mkdtemp(prefix=".anchor-guidance-", dir=target))
    installed: list[Path] = []
    try:
        shutil.copytree(sources[".assistant"], staging / ".assistant")
        shutil.copy2(sources[".assistant_instructions.md"], staging / ".assistant_instructions.md")
        for name in sources:
            destination = target / name
            os.replace(staging / name, destination)
            installed.append(destination)
    except Exception:
        for path in reversed(installed):
            shutil.rmtree(path) if path.is_dir() else path.unlink(missing_ok=True)
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return {
        "kind": "guidance_install",
        "status": "installed",
        "target_root": str(target),
        "paths": [str(target / name) for name in sources],
        "file_count": file_count,
        "content_sha256": digest.hexdigest(),
    }


__all__ = [
    "doctor",
    "install_guidance",
    "launch",
    "prepare_portfolio_runtime",
    "register_project",
]
