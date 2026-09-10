"""Explicit, installed-distribution startup and read-only diagnostics."""
from __future__ import annotations

import os
import sqlite3
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from odibi_anchor import __version__


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
    project_id: str,
    project_root: str | os.PathLike[str],
    output_format: str = "dict",
) -> Callable[..., Any]:
    """Bind one exact route and return a callable Anchor dispatcher.

    This entry point imports only package modules and therefore works from a wheel;
    it never searches for or adds a source checkout.
    """
    home = _absolute_directory(anchor_home, "ANCHOR_HOME")
    target = _absolute_directory(project_root, "ANCHOR_PROJECT_ROOT")
    if not isinstance(project_id, str) or not project_id.strip() or project_id != project_id.strip():
        raise ValueError("ANCHOR_PROJECT_ID must be a non-empty project ID")

    bindings = {
        "ANCHOR_HOME": str(home),
        "ANCHOR_PROJECT_ID": project_id,
        "ANCHOR_PROJECT_ROOT": str(target),
    }
    for name, value in bindings.items():
        current = os.environ.get(name)
        if current is not None and current != value:
            raise RuntimeError(f"{name} conflicts with the requested startup route")
    os.environ.update(bindings)

    from odibi_anchor._dispatcher._project import resolve_route_binding
    from odibi_anchor.bootstrap import init

    route = resolve_route_binding(
        home, project=project_id, target_hint=target,
        runtime_instance_id=f"startup:{os.getpid()}",
    )
    anchor, _root, _manifest = init(route_binding=route, output_format=output_format)
    if not callable(anchor):
        raise RuntimeError("bootstrap did not return a callable Anchor dispatcher")
    return anchor


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
    return {
        "kind": "startup_doctor",
        "read_only": True,
        "package": {"name": "odibi-anchor", "version": __version__},
        "home": {"path": str(paths.anchor_home), "exists": paths.anchor_home.is_dir()},
        "database": {"path": str(database), "exists": database.is_file()},
        "host": {"platform": sys.platform, "databricks": bool(env.get("DATABRICKS_RUNTIME_VERSION"))},
        "filesystem": {"resource_root": str(paths.resource_root),
                       "source_checkout": paths.source_checkout,
                       "concurrency_capability": "not_qualified"},
        "route_inputs": route_inputs,
        "routing": {"status": "exact" if route else "ambiguous_or_invalid",
                    "diagnostics": route_binding_diagnostics(route) if route else None,
                    "reason": route_error},
        "tasks": _open_tasks(database, raw_project, target),
        "concurrency": {
            "status": "unqualified",
            "implication": "doctor performs no multi-process or filesystem-locking probe; "
                           "single-writer safety must not be inferred",
        },
    }


__all__ = ["doctor", "launch"]
