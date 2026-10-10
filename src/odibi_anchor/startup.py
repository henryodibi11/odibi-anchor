"""Explicit, installed-distribution startup and read-only diagnostics."""
from __future__ import annotations

import hashlib
import importlib.metadata
import os
import re
import shutil
import sqlite3
import stat
import sys
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from odibi_anchor import __version__
from odibi_anchor._bootstrap_phases import phase, records_bootstrap_timings, timed_call

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
            else f'%pip install --no-cache-dir "odibi-anchor[databricks]=={__version__}"'
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


def _local_state_root_status(path: Path, *, compute_uid: int) -> str:
    """Classify one local root without traversing its protected contents."""
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return "absent"
    except PermissionError:
        return "inaccessible"
    if stat.S_ISLNK(metadata.st_mode):
        return "symlink"
    if not stat.S_ISDIR(metadata.st_mode):
        return "not_directory"
    if metadata.st_uid != compute_uid:
        return "different_identity"
    try:
        accessible = os.access(
            path, os.R_OK | os.W_OK | os.X_OK, effective_ids=True
        )
    except TypeError:  # pragma: no cover - Databricks is POSIX; protects portable imports.
        accessible = os.access(path, os.R_OK | os.W_OK | os.X_OK)
    return "current_identity" if accessible else "inaccessible"


def _databricks_compute_identity() -> tuple[int, str]:
    """Return the effective UID and a non-reversible OS-account fingerprint."""
    get_effective_uid = getattr(os, "geteuid", None)
    if get_effective_uid is None:  # pragma: no cover - Databricks runtimes are POSIX.
        raise RuntimeError("Databricks local state isolation requires a POSIX effective UID")
    import pwd

    compute_uid = int(get_effective_uid())
    account_name = pwd.getpwuid(compute_uid).pw_name
    fingerprint = hashlib.sha256(
        f"{compute_uid}:{account_name}".encode()
    ).hexdigest()[:12]
    return compute_uid, fingerprint


def _runtime_environment(
    environment: Mapping[str, str], *, adapter: str
) -> tuple[dict[str, str], dict[str, Any]]:
    """Resolve a physical runtime root without changing portfolio routing authority."""
    resolved = dict(environment)
    configured_root = Path(resolved["ANCHOR_HOME"])
    if adapter != "databricks":
        return resolved, {
            "configured_root": str(configured_root),
            "runtime_root": str(configured_root),
            "selection": "configured",
            "compute_uid": None,
            "compute_identity": None,
            "configured_root_status": "not_applicable",
            "migration": "not_applicable",
        }
    compute_uid, compute_identity = _databricks_compute_identity()
    configured_status = _local_state_root_status(
        configured_root, compute_uid=compute_uid
    )
    if configured_status in {"symlink", "not_directory"}:
        raise ValueError(
            "configured Databricks local_state_root must be a real directory or absent"
        )
    runtime_root = Path(
        f"{configured_root}.identity-{compute_uid}-{compute_identity}"
    )
    runtime_status = _local_state_root_status(
        runtime_root, compute_uid=compute_uid
    )
    if runtime_status not in {"absent", "current_identity"}:
        from odibi_anchor._recovery import attach_recovery

        raise attach_recovery(
            RuntimeError(
                "identity-isolated local state root is unavailable for the current "
                "Databricks compute identity"
            ),
            error_code="databricks_local_state_identity_collision",
            context={
                "configured_root": str(configured_root),
                "runtime_root": str(runtime_root),
                "compute_uid": compute_uid,
                "compute_identity": compute_identity,
                "runtime_root_status": runtime_status,
            },
        )
    resolved["ANCHOR_HOME"] = str(runtime_root)
    resolved["ANCHOR_MEMORY_DB"] = str(runtime_root / ".agent_memory.db")
    return resolved, {
        "configured_root": str(configured_root),
        "runtime_root": str(runtime_root),
        "selection": "identity_isolated",
        "compute_uid": compute_uid,
        "compute_identity": compute_identity,
        "configured_root_status": configured_status,
        "migration": "not_required",
    }


def _repository_provider_for_target(target: Path) -> Any | None:
    """Attach read-only Databricks Git Folder identity when the host can provide it."""
    if not target.as_posix().startswith("/Workspace/"):
        return None
    from odibi_anchor.operational._databricks import (
        autoconfigure_databricks_git_folder_repository,
    )

    provider, _evidence = autoconfigure_databricks_git_folder_repository(target)
    return provider


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

    route = timed_call(
        "route_binding", resolve_route_binding,
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

    try:
        from odibi_anchor.bootstrap import init

        init_kwargs: dict[str, Any] = {
            "route_binding": route,
            "output_format": output_format,
        }
        repository_provider = timed_call(
            "repository_provider", _repository_provider_for_target, target
        )
        if repository_provider is not None:
            init_kwargs["repository_provider"] = repository_provider
        anchor, _root, _manifest = timed_call("init", init, **init_kwargs)
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
    environment. It prepares the host-qualified physical local state directory and
    missing managed-project registration needed by the returned immutable binding.
    """
    from odibi_anchor.portfolio import load_portfolio_document, resolve_project

    document = load_portfolio_document(config_path)
    resolved = resolve_project(
        document["portfolio"], host_id=host_id, project_id=project_id,
        persona_id=persona_id,
    )
    adapter = document["portfolio"]["hosts"][host_id]["adapter"]
    environment, local_state = _runtime_environment(
        resolved["environment"], adapter=adapter
    )
    home = Path(environment["ANCHOR_HOME"])
    target = _absolute_directory(environment["ANCHOR_PROJECT_ROOT"], "ANCHOR_PROJECT_ROOT")
    if (
        adapter == "databricks"
        and local_state["configured_root_status"] == "current_identity"
        and not home.exists()
    ):
        configured_root = Path(local_state["configured_root"])
        try:
            configured_root.rename(home)
        except OSError as exc:
            from odibi_anchor._recovery import attach_recovery

            raise attach_recovery(
                RuntimeError(
                    "failed to migrate the accessible legacy Databricks local state root "
                    "to its identity-isolated runtime root"
                ),
                error_code="databricks_local_state_migration_failed",
                context={
                    "configured_root": str(configured_root),
                    "runtime_root": str(home),
                    "compute_uid": local_state["compute_uid"],
                    "compute_identity": local_state["compute_identity"],
                    "error_type": type(exc).__name__,
                },
            ) from exc
        local_state["migration"] = "moved_configured_root"
    if home.exists() and not home.is_dir():
        raise ValueError("configured local_state_root must be a directory")

    database = Path(environment["ANCHOR_MEMORY_DB"])
    authority_id = environment["ANCHOR_AUTHORITY_ID"]
    trust_domain = environment["ANCHOR_TRUST_DOMAIN"]
    durable_root = environment.get("ANCHOR_DURABLE_ROOT")
    is_databricks = adapter == "databricks"
    if durable_root is not None and not is_databricks and not Path(durable_root).is_dir():
        from odibi_anchor._recovery import attach_recovery

        raise attach_recovery(
            FileNotFoundError(
                "configured durable_root is unavailable; refusing to initialize or reuse local state"
            ),
            error_code="durable_root_unavailable",
            context={
                "classification": "durable_root_unavailable",
                "durable_root": durable_root,
                "databricks": False,
                "observed": "not_a_directory",
            },
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
    restore: dict[str, Any] = {
        "status": "not_applicable", "reason": "local database already exists",
        "classification": "local_present",
    }
    projects = home / "workspace" / "projects"
    snapshots: list[dict[str, Any]] = []
    if durable_root is not None and projects.exists():
        from odibi_anchor.durability import _incomplete_restore_error

        incomplete = _incomplete_restore_error(projects)
        if incomplete is not None:
            raise incomplete
    if durable_root is not None and database.exists() != projects.exists():
        from odibi_anchor.durability import list_snapshots

        try:
            snapshots = list_snapshots(
                durable_root=durable_root,
                authority_id=authority_id,
                databricks=is_databricks,
            )["snapshots"]
        except FileNotFoundError as exc:
            if getattr(exc, "error_code", None) == "durable_root_unavailable":
                raise
            snapshots = []
        latest_is_v2 = bool(
            snapshots and snapshots[-1].get("format") == "odibi-anchor-durable-snapshot-v2"
        )
        if latest_is_v2:
            raise RuntimeError(
                "local durable state is partial: the database and managed projects must both "
                "exist or both be absent before portfolio preparation"
            )
    if not database.exists():
        if durable_root is not None:
            from odibi_anchor.durability import (
                DurableSnapshotUnavailable,
                restore_latest,
            )

            # Only confirmed first use initializes empty state; an unreachable root, a
            # lineage whose snapshots vanished, or an unfinished restore fails closed.
            try:
                restore = timed_call(
                    "restore", restore_latest,
                    durable_root=durable_root,
                    destination_db=database,
                    destination_artifacts=projects,
                    authority_id=authority_id,
                    databricks=is_databricks,
                )
            except DurableSnapshotUnavailable:
                restore = {
                    "status": "not_applicable", "reason": "no durable snapshot exists",
                    "classification": "no_lineage",
                }
        else:
            restore = {
                "status": "not_applicable", "reason": "no durable snapshot exists",
                "classification": "not_configured",
            }
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
    except ValueError as exc:
        # Surface structured route failures unchanged, adding the portfolio provenance.
        if getattr(exc, "error_code", None) in {
            "route_target_conflict", "managed_descriptor_damaged",
        }:
            exc.context.update(config_path=document["path"], host_id=host_id)  # type: ignore[attr-defined]
        if getattr(exc, "error_code", None) == "managed_descriptor_damaged":
            from odibi_anchor._dispatcher._descriptor import attach_repair_recovery

            attach_repair_recovery(exc, config_path=document["path"], host_id=host_id)
        raise
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
        "local_state": local_state,
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


def _managed_host_id(
    portfolio: Mapping[str, Any],
    *,
    instruction_root: Path,
    host_id: str | None,
) -> str:
    """Resolve one host from explicit input or the launcher's exact instruction root."""
    hosts = portfolio.get("hosts")
    if not isinstance(hosts, Mapping):
        raise ValueError("portfolio hosts are unavailable")
    if host_id is not None:
        if host_id not in hosts:
            raise ValueError(f"host is not configured: {host_id}")
        selected = hosts[host_id]
        configured_root = selected.get("instruction_root")
        if configured_root is not None and Path(configured_root).resolve() != instruction_root:
            raise ValueError(
                f"host {host_id} instruction_root does not match the managed launcher"
            )
        return host_id
    matches = [
        candidate
        for candidate, settings in hosts.items()
        if isinstance(settings, Mapping)
        and settings.get("instruction_root") is not None
        and Path(settings["instruction_root"]).resolve() == instruction_root
    ]
    if not matches:
        raise ValueError("no portfolio host matches the managed launcher instruction root")
    if len(matches) != 1:
        raise ValueError("multiple portfolio hosts match the managed launcher instruction root")
    return str(matches[0])


def repair_portfolio_descriptor(
    *, config_path: str | os.PathLike[str], host_id: str, project_id: str,
    expected_sha256: Any, approve: Any = False,
    anchor_home: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Repair one damaged ``PROJECT.md`` under the portfolio's host/project authority.

    The portfolio target is the only target written. ``approve=False`` returns the exact
    dry-run plan. This never restores, registers, binds or selects a project. With
    ``anchor_home`` (a running dispatcher), the portfolio host's runtime root must be it.
    """
    from odibi_anchor._dispatcher._project import repair_descriptor
    from odibi_anchor._recovery import attach_recovery
    from odibi_anchor.portfolio import load_portfolio_document, resolve_project

    document = load_portfolio_document(config_path)
    authority = {
        "config_path": document["path"], "config_sha256": document["sha256"], "host_id": host_id,
    }
    try:
        resolved = resolve_project(document["portfolio"], host_id=host_id, project_id=project_id)
    except ValueError as exc:
        raise attach_recovery(
            ValueError(
                f"Managed project '{project_id}' descriptor repair refused "
                f"(descriptor_repair_refused, portfolio_mismatch): {exc}. PROJECT.md was not "
                "changed; Anchor never guesses a target. Stop and ask the project owner."
            ),
            error_code="descriptor_repair_refused",
            context={
                **authority, "project_id": project_id, "classification": "portfolio_mismatch",
                "reason": str(exc),
            },
        ) from exc
    adapter = document["portfolio"]["hosts"][host_id]["adapter"]
    environment, _local_state = _runtime_environment(resolved["environment"], adapter=adapter)
    home = environment["ANCHOR_HOME"]
    if anchor_home is not None and Path(anchor_home).resolve() != Path(home).resolve():
        raise attach_recovery(
            ValueError(
                f"Managed project '{project_id}' descriptor repair refused "
                "(descriptor_repair_refused, portfolio_mismatch): the portfolio host runtime "
                f"root {home!r} is not this runtime's ANCHOR_HOME {str(anchor_home)!r}. "
                "PROJECT.md was not changed. Stop and ask the project owner."
            ),
            error_code="descriptor_repair_refused",
            context={
                **authority, "project_id": project_id, "classification": "portfolio_mismatch",
                "reason": "portfolio host runtime root differs from this runtime",
                "portfolio_anchor_home": home, "anchor_home": str(anchor_home),
            },
        )
    return repair_descriptor(
        home, project_id, portfolio_target=resolved["target_root"],
        expected_sha256=expected_sha256, approve=approve, authority=authority,
    )


@records_bootstrap_timings
def bootstrap_managed_project(
    *,
    config_path: str | os.PathLike[str],
    project_id: str,
    instruction_root: str | os.PathLike[str],
    host_id: str | None = None,
    create_if_missing: bool = False,
    project_root: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Prepare, bind, orient, and describe one portfolio-managed runtime.

    Existing projects need only their ID. Creation is a separate explicit mode and
    requires an exact existing target; successful creation is durably checkpointed
    when the selected host configures durable storage. The startup packet's
    ``timings`` reports each phase's elapsed milliseconds and outcome; a failure
    carries the same summary as ``exc.bootstrap_timings``.
    """
    from odibi_anchor.portfolio import add_project, load_portfolio_document, resolve_project

    root = _absolute_directory(instruction_root, "instruction_root")
    if not isinstance(project_id, str) or re.fullmatch(
        r"[a-z0-9]+(?:-[a-z0-9]+)*", project_id
    ) is None:
        raise ValueError("project_id must be a canonical lowercase hyphenated project ID")
    document = timed_call("portfolio_load", load_portfolio_document, config_path)
    portfolio = document["portfolio"]
    selected_host = _managed_host_id(
        portfolio, instruction_root=root, host_id=host_id
    )
    host = portfolio["hosts"][selected_host]

    from odibi_anchor.host_setup import setup_host

    with phase("host_guidance") as guidance_phase:
        guidance_options: dict[str, Any] = {"adapter": host["adapter"]}
        if host["adapter"] == "databricks":
            uid, identity = _databricks_compute_identity()
            # Keep the cache compute-local but outside the live state tree: creating
            # that tree here would interfere with subsequent cold restore/migration.
            guidance_options["receipt_root"] = (
                f"{host['local_state_root']}.guidance-{uid}-{identity}"
            )
        guidance = setup_host(root, **guidance_options)
        guidance_phase["verification"] = guidance.get("verification", "content")

    def refuse_environment_conflicts(environment: Mapping[str, str]) -> None:
        optional_managed_names = {
            "ANCHOR_DURABLE_ROOT",
            "ANCHOR_RETENTION_DAYS",
            "ANCHOR_RETENTION_MINIMUM_SNAPSHOTS",
        }
        conflicts = {
            name
            for name in set(environment) | optional_managed_names
            if name in os.environ and os.environ[name] != environment.get(name)
        }
        if conflicts:
            raise RuntimeError(
                "managed bootstrap environment conflicts with this Python process; "
                "restart Python before binding another project: "
                + ", ".join(sorted(conflicts))
            )

    created = False
    portfolio_change: dict[str, Any] | None = None
    target: Path | None = None
    if project_id not in portfolio.get("projects", {}):
        if not create_if_missing:
            raise RuntimeError(
                f"managed project {project_id!r} is not configured; creation requires "
                "explicit user authorization and one exact project_root"
            )
        if project_root is None:
            raise ValueError("project_root is required when create_if_missing is true")
        target = _absolute_directory(project_root, "project_root")
        expected_environment, _expected_local_state = timed_call(
            "local_state_identity", _runtime_environment, {
            "ANCHOR_HOME": host["local_state_root"],
            "ANCHOR_MEMORY_DB": os.path.join(
                host["local_state_root"], ".agent_memory.db"
            ).replace("\\", "/"),
            "ANCHOR_AUTHORITY_ID": portfolio["authority"]["id"],
            "ANCHOR_TRUST_DOMAIN": portfolio["authority"]["trust_domain"],
            "ANCHOR_PROJECT_ID": project_id,
            "ANCHOR_PROJECT_ROOT": str(target),
            **(
                {"ANCHOR_DURABLE_ROOT": host["durable_root"]}
                if host.get("durable_root")
                else {}
            ),
            **(
                {
                    "ANCHOR_RETENTION_DAYS": str(
                        portfolio["durability"]["retention"]["days"]
                    ),
                    "ANCHOR_RETENTION_MINIMUM_SNAPSHOTS": str(
                        portfolio["durability"]["retention"]["minimum_snapshots"]
                    ),
                }
                if portfolio.get("durability", {}).get("retention") is not None
                else {}
            ),
        }, adapter=host["adapter"])
        refuse_environment_conflicts(expected_environment)
        portfolio_change = add_project(
            config_path,
            project_id=project_id,
            host_id=selected_host,
            target_root=str(target),
            expected_sha256=document["sha256"],
        )
        created = True
    elif create_if_missing:
        raise ValueError(f"managed project already exists: {project_id}")
    else:
        expected_environment, _expected_local_state = timed_call(
            "local_state_identity", _runtime_environment,
            resolve_project(
                portfolio, host_id=selected_host, project_id=project_id
            )["environment"],
            adapter=host["adapter"],
        )
        refuse_environment_conflicts(expected_environment)

    prepared = timed_call(
        "runtime_preparation", prepare_portfolio_runtime,
        config_path=config_path,
        host_id=selected_host,
        project_id=project_id,
    )
    if prepared["environment"] != expected_environment:
        raise RuntimeError("portfolio environment changed during managed bootstrap preparation")
    os.environ.update(prepared["environment"])
    anchor = launch(
        anchor_home=prepared["environment"]["ANCHOR_HOME"],
        project_id=project_id,
        project_root=prepared["target_root"],
        output_format="dict",
    )
    orientation = timed_call("orient", anchor, "orient", output_format="dict")
    if not isinstance(orientation, Mapping) or orientation.get("kind") != "orientation":
        raise RuntimeError("Odibi Anchor orientation returned an invalid structured result")
    status = orientation.get("status")
    runtime = status.get("runtime") if isinstance(status, Mapping) else None
    binding = runtime.get("route_binding") if isinstance(runtime, Mapping) else None
    if (
        not isinstance(binding, Mapping)
        or binding.get("project_id") != project_id
        or binding.get("target_root") != prepared["target_root"]
        or not binding.get("artifact_root")
    ):
        raise RuntimeError("managed bootstrap orientation does not match the requested project")

    durable_checkpoint: dict[str, Any] | None = None
    durable_root = prepared["environment"].get("ANCHOR_DURABLE_ROOT")
    if created and durable_root is not None:
        from odibi_anchor.durability import snapshot_state

        home = Path(prepared["environment"]["ANCHOR_HOME"])
        durable_checkpoint = timed_call(
            "durable_checkpoint", snapshot_state,
            source_db=prepared["environment"]["ANCHOR_MEMORY_DB"],
            source_artifacts=home / "workspace" / "projects",
            durable_root=durable_root,
            authority_id=prepared["environment"]["ANCHOR_AUTHORITY_ID"],
            databricks=host["adapter"] == "databricks",
            retention_days=(
                int(prepared["environment"]["ANCHOR_RETENTION_DAYS"])
                if "ANCHOR_RETENTION_DAYS" in prepared["environment"]
                else None
            ),
            minimum_snapshots=(
                int(prepared["environment"]["ANCHOR_RETENTION_MINIMUM_SNAPSHOTS"])
                if "ANCHOR_RETENTION_MINIMUM_SNAPSHOTS" in prepared["environment"]
                else None
            ),
        )

    from odibi_anchor._dispatcher._descriptor import read_descriptor

    try:
        bound_descriptor = read_descriptor(binding["artifact_root"])
        descriptor_summary = {
            "integrity_status": bound_descriptor.status,
            "defaulted_fields": list(bound_descriptor.defaulted_fields),
        }
    except FileNotFoundError:
        descriptor_summary = {"integrity_status": "absent", "defaulted_fields": []}
    startup_packet = {
        "kind": "managed_startup_packet",
        "status": "ready",
        "project_id": project_id,
        "host_id": selected_host,
        "target_root": prepared["target_root"],
        "artifact_root": binding.get("artifact_root"),
        "binding_source": binding.get("binding_source"),
        # Visible when routing depends on defaulted descriptor fields (id, project_type).
        "descriptor": descriptor_summary,
        "guidance": {
            "status": guidance.get("status"),
            "verified_files": guidance.get("verified_file_count"),
            "verified_skills": guidance.get("verified_skill_count"),
            **({"verification": guidance["verification"]} if "verification" in guidance else {}),
        },
        "local_state": prepared["local_state"],
        "restore": {
            key: prepared["restore"][key]
            for key in (
                "status", "action", "classification", "reason", "snapshot_id", "sha256",
                "format", "integrity_check", "continuity",
            )
            if key in prepared["restore"]
        },
        "project_created": created,
        "portfolio_change": (
            {
                key: portfolio_change[key]
                for key in ("status", "path", "sha256", "project_id", "host_id", "target_root")
                if key in portfolio_change
            }
            if portfolio_change is not None
            else None
        ),
        "durable_checkpoint": (
            {
                key: durable_checkpoint[key]
                for key in ("status", "action", "snapshot_id", "sha256", "format")
                if key in durable_checkpoint
            }
            if durable_checkpoint is not None
            else None
        ),
        "managed_artifact_actions": orientation.get("managed_artifact_actions", []),
        "memory_scope_semantics": {
            "project_local": "eligible only inside its exact managed project",
            "all": "eligible across projects when relevant; not selected for every task",
        },
        "next_required_action": (orientation.get("metrics") or {}).get(
            "next_required_action"
        ),
    }
    return {
        "kind": "managed_project_runtime",
        "status": "ready",
        "anchor": anchor,
        "root": prepared["target_root"],
        "manifest": getattr(anchor, "manifest", None),
        "orientation": orientation,
        "preparation": prepared,
        "startup_packet": startup_packet,
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


_DESCRIPTOR_READ_LIMIT = 1_048_576


def _stop_for_owner(reason: str) -> dict[str, Any]:
    return {
        "operation": "stop_and_ask_owner",
        "reason": reason,
        "requires_owner": True,
        "retry_safety": "read_only",
    }


def _error_step(name: str, exc: BaseException, **facts: Any) -> dict[str, Any]:
    """Describe one blocking failure with its structured recovery, never a guessed fix."""
    return {
        "step": name,
        "status": "blocked",
        **facts,
        "error": {
            "type": type(exc).__name__,
            "message": str(exc),
            "error_code": getattr(exc, "error_code", None),
            "context": getattr(exc, "context", None),
        },
        "next_operation": getattr(exc, "next_operation", None)
        or _stop_for_owner("This failure has no supported automatic correction."),
    }


def _launcher_discovery(launcher_root: Path, config: Path, adapter: str) -> dict[str, Any]:
    """Replay the managed launcher's portfolio precedence read-only for one instruction root."""
    from odibi_anchor.host_setup import HostSetupError, read_host_binding

    default = launcher_root / ".odibi-anchor" / "anchor.toml"
    nested = launcher_root.name == ".assistant"
    record = {
        "operation": "setup_host.record_portfolio",
        "copy_ready": (
            f"anchor setup-host {adapter} --target {launcher_root.as_posix()} "
            f"--portfolio {config.as_posix()}"
        ),
        "reason": "Record this exact portfolio in the host binding sidecar for future launches.",
        "requires_owner": True,
        "retry_safety": "idempotent",
    }
    facts: dict[str, Any] = {
        "instruction_root": launcher_root.as_posix(),
        "default_path": default.as_posix(),
        "default_exists": default.is_file(),
        "nested_install": nested,
    }
    if "ANCHOR_PORTFOLIO_CONFIG" in os.environ:
        selected = Path(os.environ["ANCHOR_PORTFOLIO_CONFIG"])
        source = "ANCHOR_PORTFOLIO_CONFIG environment variable"
    else:
        try:
            binding = read_host_binding(launcher_root)
        except (HostSetupError, OSError) as exc:
            return {**facts, "status": "blocked", "source": "host binding sidecar",
                    "reason": str(exc), "next_operation": record}
        if binding is not None:
            selected = Path(binding["config_path"])
            source = "host binding sidecar"
            facts["binding"] = binding
            if default.is_file() and default.resolve() != selected.resolve():
                return {**facts, "status": "blocked", "source": source,
                        "selected_path": selected.as_posix(),
                        "reason": "managed_portfolio_ambiguous: the sidecar and default differ",
                        "next_operation": record}
        else:
            selected = default
            source = "instruction-root default"
    matches = selected.is_file() and selected.resolve() == config.resolve()
    facts.update(source=source, selected_path=selected.as_posix(), selects_config=matches)
    if matches and not nested:
        return {**facts, "status": "ok", "next_operation": None}
    return {
        **facts,
        "status": "action_required",
        "reason": (
            "nested .assistant install: the launcher's instruction root is itself named .assistant"
            if nested
            else "the launcher would not select this portfolio without explicit input"
        ),
        "next_operation": record,
    }


def _guidance_step(launcher_root: Path, adapter: str) -> dict[str, Any]:
    """Classify host guidance with the read-only reconcile dry run."""
    from odibi_anchor.host_setup import HostSetupError, setup_host

    try:
        plan = setup_host(launcher_root, adapter=adapter, reconcile=True, dry_run=True)
    except (HostSetupError, OSError, ValueError) as exc:
        return _error_step("host_guidance", exc, instruction_root=launcher_root.as_posix())
    files = plan["files"]
    # Plain setup during bootstrap installs new files and upgrades files that still match
    # the manifest; anything else is drift that bootstrap refuses. Legacy hosts without a
    # manifest are reported conservatively: reconcile classifies their released bytes.
    pending = [
        entry["path"] for entry in files
        if not (
            entry["classification"] == "current"
            or (entry["manifest_sha256"] is not None and entry["manifest_match"])
            or (entry["classification"] == "missing" and entry["manifest_sha256"] is None)
        )
    ]
    facts = {
        "instruction_root": launcher_root.as_posix(),
        "reconcile_status": plan["status"],
        "classification_counts": plan["classification_counts"],
        "drifted_files": [
            {key: entry[key] for key in ("path", "classification", "action", "released_versions")}
            for entry in files if entry["classification"] != "current"
        ],
        "approval_required": plan["approval_required"],
    }
    if pending:
        return {"step": "host_guidance", "status": "blocked", **facts,
                "reason": "bootstrap refuses this drift (host_guidance_drift)",
                "next_operation": plan["next_operation"]}
    effect = "none" if plan["status"] == "unchanged" else "bootstrap installs or upgrades unchanged managed files"
    return {"step": "host_guidance", "status": "ok", **facts, "launch_effect": effect,
            "next_operation": None}


def _snapshot_descriptor(
    *, durable_root: str, authority_id: str, databricks: bool, snapshot: Mapping[str, Any],
    project_id: str,
) -> bytes | None:
    """Read one project's PROJECT.md from a verified v2 artifact bundle, in memory."""
    import tarfile

    from odibi_anchor import durability

    artifacts = snapshot["artifacts"]

    def member(bundle: Path) -> bytes | None:
        digest = hashlib.sha256()
        with bundle.open("rb") as stream:
            for block in iter(lambda: stream.read(1 << 20), b""):
                digest.update(block)
        if digest.hexdigest() != artifacts["sha256"]:
            raise RuntimeError(f"artifact bundle hash mismatch: {artifacts['file']}")
        with tarfile.open(bundle, mode="r:") as archive:
            try:
                info = archive.getmember(f"{project_id}/PROJECT.md")
            except KeyError:
                return None
            if not info.isfile() or info.size > _DESCRIPTOR_READ_LIMIT:
                raise RuntimeError("snapshot PROJECT.md is not a bounded regular file")
            handle = archive.extractfile(info)
            return None if handle is None else handle.read()

    if not databricks:
        return member(Path(durable_root) / authority_id / "snapshots" / artifacts["file"])
    files = durability._databricks_files_api()
    remote = durability._remote_child(
        durability._remote_child(durability._remote_child(durable_root, authority_id), "snapshots"),
        artifacts["file"],
    )
    with tempfile.TemporaryDirectory(prefix="odibi-anchor-doctor-") as temporary:
        local = Path(temporary) / artifacts["file"]
        files.download_to(remote, str(local), overwrite=False, use_parallel=False)
        return member(local)


def _lineage_step(
    environment: Mapping[str, str], local_state: Mapping[str, Any], *, databricks: bool,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Classify durable lineage with existing read helpers; return the latest snapshot if any."""
    from odibi_anchor import durability

    home = Path(environment["ANCHOR_HOME"])
    database = Path(environment["ANCHOR_MEMORY_DB"])
    durable_root = environment.get("ANCHOR_DURABLE_ROOT")
    authority_id = environment["ANCHOR_AUTHORITY_ID"]
    projects = home / "workspace" / "projects"
    facts: dict[str, Any] = {
        "runtime_root": home.as_posix(),
        "local_state": dict(local_state),
        "durable_root": durable_root,
        "authority_id": authority_id,
    }
    migrating = local_state.get("configured_root_status") == "current_identity" and not home.exists()
    if migrating:
        facts["launch_effect"] = "bootstrap moves the accessible legacy local state root"
        database = Path(local_state["configured_root"]) / ".agent_memory.db"
        projects = Path(local_state["configured_root"]) / "workspace" / "projects"
    if durable_root is not None and projects.exists():
        incomplete = durability._incomplete_restore_error(projects)
        if incomplete is not None:
            return _error_step("durable_lineage", incomplete, classification="restore_incomplete", **facts), None
    if database.exists():
        return {"step": "durable_lineage", "status": "ok", "classification": "local_present", **facts,
                "next_operation": None}, None
    if durable_root is None:
        return {"step": "durable_lineage", "status": "ok", "classification": "not_configured", **facts,
                "launch_effect": "bootstrap initializes empty local state", "next_operation": None}, None
    try:
        snapshots = durability.list_snapshots(
            durable_root=durable_root, authority_id=authority_id, databricks=databricks,
        )["snapshots"]
    except FileNotFoundError as exc:
        if getattr(exc, "error_code", None) == "durable_root_unavailable":
            return _error_step("durable_lineage", exc, classification="durable_root_unavailable", **facts), None
        snapshots = []
    except Exception as exc:
        if getattr(exc, "error_code", None) == "durable_root_unavailable":
            return _error_step("durable_lineage", exc, classification="durable_root_unavailable", **facts), None
        return _error_step("durable_lineage", exc, classification="durable_lineage_unreadable", **facts), None
    if not snapshots:
        files = durability._databricks_files_api() if databricks else None
        empty = durability._no_snapshots(
            Path(durable_root), authority_id, files=files, databricks=databricks
        )
        if isinstance(empty, durability.DurableSnapshotUnavailable):
            return {"step": "durable_lineage", "status": "ok", "classification": "no_lineage", **facts,
                    "launch_effect": "first use: bootstrap initializes empty local state",
                    "next_operation": None}, None
        return _error_step("durable_lineage", empty, classification="durable_lineage_missing", **facts), None
    latest = snapshots[-1]
    return {
        "step": "durable_lineage", "status": "ok", "classification": "restored", **facts,
        "snapshot": {key: latest.get(key) for key in ("snapshot_id", "created_at", "format", "sha256")},
        "launch_effect": "bootstrap restores this latest verified snapshot",
        "next_operation": None,
    }, latest


def _route_step(
    *, project_id: str, portfolio_target: str, artifact_root: Path, lineage: Mapping[str, Any],
    snapshot: Mapping[str, Any] | None, environment: Mapping[str, str], databricks: bool,
) -> dict[str, Any]:
    """Compare the descriptor launch will use with the portfolio target via the route classifier."""
    from odibi_anchor._dispatcher._descriptor import parse_descriptor_text, read_descriptor
    from odibi_anchor._dispatcher._project import (
        _descriptor_target,
        _route_target_conflict,
        descriptor_damaged_error,
        route_path_identity,
    )

    facts: dict[str, Any] = {
        "portfolio_target": portfolio_target, "artifact_root": artifact_root.as_posix(),
    }
    classification = lineage.get("classification")
    integrity = None
    try:
        if classification == "local_present" and (artifact_root / "PROJECT.md").is_file():
            integrity = read_descriptor(artifact_root)
            facts["descriptor_source"] = "local_state"
        elif classification == "restored" and snapshot is not None and snapshot.get("artifacts"):
            content = _snapshot_descriptor(
                durable_root=environment["ANCHOR_DURABLE_ROOT"],
                authority_id=environment["ANCHOR_AUTHORITY_ID"], databricks=databricks,
                snapshot=snapshot, project_id=project_id,
            )
            facts["descriptor_source"] = f"snapshot:{snapshot['snapshot_id']}"
            if content is not None:
                try:
                    text = content.decode("utf-8")
                except UnicodeDecodeError:
                    text = "\ufffd"
                # Parse at the path restore will publish: a defaulted project_type is
                # derived from the descriptor's parent, the runtime artifact root.
                integrity = parse_descriptor_text(
                    text, path=str(artifact_root / "PROJECT.md"),
                    sha256=hashlib.sha256(content).hexdigest(), expected_id=project_id,
                )
    except (OSError, RuntimeError, ValueError) as exc:
        return _error_step("route_comparison", exc, **facts)
    if integrity is None:
        facts.setdefault("descriptor_source", "none")
        return {"step": "route_comparison", "status": "ok", "classification": "unregistered", **facts,
                "launch_effect": "bootstrap registers the portfolio target", "next_operation": None}
    facts["descriptor_sha256"] = integrity.sha256
    if not integrity.intact:
        return _error_step(
            "route_comparison",
            descriptor_damaged_error(integrity, project_id=project_id, artifact_root=artifact_root.as_posix()),
            classification="descriptor_damaged", **facts,
        )
    descriptor_target = _descriptor_target(artifact_root, integrity)
    facts.update(
        descriptor_target=descriptor_target,
        descriptor_project_type=integrity.fields["project_type"],
        descriptor_defaulted_fields=list(integrity.defaulted_fields),
    )
    if route_path_identity(descriptor_target) == route_path_identity(portfolio_target):
        return {"step": "route_comparison", "status": "ok", "classification": "match", **facts,
                "next_operation": None}
    conflict = _route_target_conflict(
        {"project_id": project_id, "target_root": descriptor_target,
         "artifact_root": artifact_root.as_posix()},
        portfolio_target,
    )
    return _error_step(
        "route_comparison", conflict,
        classification=getattr(conflict, "context", {}).get("classification"), **facts,
    )


def _fresh_compute_plan(*, config_path: str | os.PathLike[str], host_id: str, project_id: str) -> dict[str, Any]:
    """Walk a fresh compute identity's launch read-only and name each exact next operation."""
    from odibi_anchor.portfolio import load_portfolio_document, resolve_project

    steps: list[dict[str, Any]] = []
    plan: dict[str, Any] = {
        "kind": "fresh_compute_plan",
        "read_only": True,
        "writes": "none; remote snapshot reads stage downloads in self-deleting temporary directories",
        "package": {"name": "odibi-anchor", "version": __version__},
        "config_path": os.fspath(config_path),
        "host_id": host_id,
        "project_id": project_id,
        "steps": steps,
    }
    try:
        document = load_portfolio_document(config_path)
        portfolio = document["portfolio"]
        if host_id not in portfolio.get("hosts", {}):
            raise ValueError(f"host is not configured: {host_id}")
        resolved = resolve_project(portfolio, host_id=host_id, project_id=project_id)
    except (OSError, ValueError) as exc:
        steps.append(_error_step("portfolio_discovery", exc))
        steps[-1]["next_operation"] = {
            "operation": "portfolio.validate",
            "copy_ready": f"anchor portfolio validate --config {os.fspath(config_path)} --host {host_id}",
            "reason": "Inspect the exact portfolio, host and project read-only before any change.",
            "requires_owner": False,
            "retry_safety": "read_only",
        }
        return _finish_plan(plan)
    host = portfolio["hosts"][host_id]
    adapter = host["adapter"]
    databricks = adapter == "databricks"
    environment_inputs = resolved["environment"]
    portfolio_target = environment_inputs["ANCHOR_PROJECT_ROOT"]
    launcher_root = Path(host.get("instruction_root") or portfolio_target)
    config = Path(document["path"])
    discovery = _launcher_discovery(launcher_root, config, adapter)
    steps.append({"step": "portfolio_discovery", "config_sha256": document["sha256"],
                  "adapter": adapter, **discovery})
    steps.append(_guidance_step(launcher_root, adapter))
    try:
        environment, local_state = _runtime_environment(environment_inputs, adapter=adapter)
    except (OSError, RuntimeError, ValueError) as exc:
        steps.append(_error_step("durable_lineage", exc))
        environment = None
    lineage: dict[str, Any] | None = None
    if environment is not None:
        lineage, snapshot = _lineage_step(environment, local_state, databricks=databricks)
        steps.append(lineage)
        if lineage["status"] == "ok":
            steps.append(_route_step(
                project_id=project_id, portfolio_target=portfolio_target,
                artifact_root=Path(environment["ANCHOR_HOME"]) / "workspace" / "projects" / project_id,
                lineage=lineage, snapshot=snapshot, environment=environment, databricks=databricks,
            ))
    if lineage is None or lineage["status"] != "ok":
        steps.append({"step": "route_comparison", "status": "not_evaluated",
                      "reason": "durable lineage must be classified first", "next_operation": None})
    init_globals = {"ANCHOR_PROJECT_ID": project_id}
    if not discovery.get("selects_config"):
        init_globals["ANCHOR_PORTFOLIO_CONFIG"] = config.as_posix()
    launcher = launcher_root / ".assistant" / "agent_bootstrap.py"
    plan["launch_inputs"] = {
        "launcher": launcher.as_posix(),
        "init_globals": init_globals,
        "copy_ready": f"runpy.run_path({launcher.as_posix()!r}, init_globals={init_globals!r})",
        "target_root": portfolio_target,
        "runtime_root": None if environment is None else environment["ANCHOR_HOME"],
    }
    return _finish_plan(plan)


def _finish_plan(plan: dict[str, Any]) -> dict[str, Any]:
    steps = plan["steps"]
    blocked = [step for step in steps if step["status"] == "blocked"]
    pending = [step for step in steps if step["status"] == "action_required"]
    launch = plan.get("launch_inputs")
    status = "blocked" if blocked or launch is None else "action_required" if pending else "ready"
    steps.append({
        "step": "launch_inputs",
        "status": "blocked" if status == "blocked" else "ok",
        **({} if launch is None else launch),
        "next_operation": None if status == "blocked" or launch is None else {
            "operation": "run_managed_launcher",
            "copy_ready": launch["copy_ready"],
            "reason": "Bootstrap in the persistent Python process with these exact inputs.",
            "requires_owner": False,
            "retry_safety": "idempotent",
        },
    })
    plan["status"] = status
    first = (blocked or pending or steps[-1:])[0]
    plan["next_operation"] = first["next_operation"]
    plan["next_step"] = first["step"]
    return plan


def doctor(
    *,
    environment: Mapping[str, str] | None = None,
    fresh_compute: bool = False,
    config_path: str | os.PathLike[str] | None = None,
    host_id: str | None = None,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Return secret-free startup facts without creating or changing state.

    ``fresh_compute=True`` with ``config_path``, ``host_id`` and ``project_id`` returns a
    read-only plan for launching that project on a fresh compute identity.
    """
    if fresh_compute:
        if config_path is None or host_id is None or project_id is None:
            raise ValueError("fresh_compute requires config_path, host_id and project_id")
        return _fresh_compute_plan(config_path=config_path, host_id=host_id, project_id=project_id)
    if config_path is not None or host_id is not None or project_id is not None:
        raise ValueError("config_path, host_id and project_id apply only with fresh_compute=True")
    env = os.environ if environment is None else environment
    from odibi_anchor._dispatcher._project import resolve_route_binding, route_binding_diagnostics
    from odibi_anchor._runtime_paths import resolve_resource_root, resolve_runtime_paths

    raw_project = env.get("ANCHOR_PROJECT_ID")
    raw_target = env.get("ANCHOR_PROJECT_ROOT")
    route_inputs = {
        "ANCHOR_HOME": env.get("ANCHOR_HOME"),
        "ANCHOR_PROJECT_ID": raw_project,
        "ANCHOR_PROJECT_ROOT": raw_target,
    }
    is_databricks = bool(env.get("DATABRICKS_RUNTIME_VERSION"))
    databricks_capability = _databricks_capability(required=is_databricks)
    if is_databricks and not env.get("ANCHOR_HOME"):
        resource_root = resolve_resource_root()
        next_operation = (
            {
                "operation": "install_dependency",
                "command": databricks_capability["install_command"],
                "restart_python": True,
                "reason": "Databricks durability requires the qualified Workspace Files API SDK.",
            }
            if not databricks_capability["qualified"]
            else {
                "operation": "portfolio.prepare",
                "required_inputs": ["config_path", "host_id", "project_id"],
                "command": (
                    "anchor portfolio prepare --config <absolute-config> "
                    "--host <host-id> --project <project-id>"
                ),
                "reason": (
                    "Portfolio preparation selects local live state, durable snapshots, "
                    "and the exact managed-project route. Do not set ANCHOR_HOME manually."
                ),
            }
        )
        return {
            "kind": "startup_doctor",
            # One top-level summary: routing is not ready until portfolio preparation runs.
            "status": "attention",
            "read_only": True,
            "package": {"name": "odibi-anchor", "version": __version__},
            "home": {"path": None, "exists": False, "status": "unconfigured"},
            "database": {"path": None, "exists": False, "status": "unconfigured"},
            "host": {"platform": sys.platform, "databricks": True},
            "capabilities": {"databricks_sdk": databricks_capability},
            "filesystem": {
                "resource_root": str(resource_root),
                "source_checkout": (resource_root / "src" / "odibi_anchor").is_dir(),
                "concurrency_capability": "not_qualified",
            },
            "route_inputs": route_inputs,
            "routing": {
                "status": "unconfigured",
                "diagnostics": None,
                "reason": "portfolio preparation has not supplied the exact Databricks route",
            },
            "next_operation": next_operation,
            "tasks": {
                "status": "unavailable",
                "count": None,
                "implication": "portfolio preparation is required before task inspection",
            },
            "concurrency": {
                "status": "unqualified",
                "implication": "doctor performs no multi-process or filesystem-locking probe; "
                               "single-writer safety must not be inferred",
            },
        }

    paths = resolve_runtime_paths(environment=env)
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
        # One top-level summary: ready only when the route is exact and, on Databricks, the
        # SDK is qualified; details stay in routing and capabilities.
        "status": "ready" if route and (not is_databricks or databricks_capability["qualified"]) else "attention",
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
