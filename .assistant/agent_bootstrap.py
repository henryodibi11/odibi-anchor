"""Resolve one Odibi Anchor checkout and run its authoritative bootstrap."""

from __future__ import annotations

if "__file__" not in globals():
    raise RuntimeError(
        "Odibi Anchor bootstrap requires __file__; use "
        "runpy.run_path(<exact .assistant/agent_bootstrap.py>) in the persistent Python process"
    )

import json
import os
import re
import runpy
import urllib.request
from collections.abc import Mapping
from importlib import metadata
from pathlib import Path


def _validated_checkout(raw: object, *, source: str) -> Path:
    if not isinstance(raw, (str, os.PathLike)) or isinstance(raw, bytes):
        raise RuntimeError(f"{source} must be an absolute, non-tilde path")
    value = os.fspath(raw)
    if not isinstance(value, str) or not value or value.startswith("~") or not Path(value).is_absolute():
        raise RuntimeError(f"{source} must be an absolute, non-tilde path")
    checkout = Path(value).resolve(strict=False)
    markers = (
        checkout / "agent_bootstrap.py",
        checkout / "src" / "odibi_anchor" / "bootstrap.py",
    )
    if not checkout.is_dir() or not all(marker.is_file() for marker in markers):
        raise RuntimeError(
            f"{source} is not a Odibi Anchor source checkout: {checkout}; "
            "expected agent_bootstrap.py and src/odibi_anchor/bootstrap.py"
        )
    return checkout.resolve(strict=True)


try:
    _launcher = Path(__file__).resolve(strict=True)
except (OSError, TypeError) as exc:
    raise RuntimeError("Odibi Anchor launcher __file__ is invalid") from exc


def _latest_stable_release() -> str:
    """Resolve the newest non-yanked stable release from the public package index."""
    request = urllib.request.Request(
        "https://pypi.org/pypi/odibi-anchor/json",
        headers={"Accept": "application/json", "User-Agent": "odibi-anchor-managed-launcher"},
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.load(response)
    except Exception as exc:
        raise RuntimeError(
            "cannot verify the latest stable odibi-anchor release from PyPI; "
            "retry when package-index access is available"
        ) from exc
    releases = payload.get("releases") if isinstance(payload, dict) else None
    if not isinstance(releases, dict):
        raise RuntimeError("PyPI returned an invalid odibi-anchor release index")
    candidates: list[tuple[tuple[int, int, int], str]] = []
    for version, files in releases.items():
        if not isinstance(version, str) or re.fullmatch(r"\d+\.\d+\.\d+", version) is None:
            continue
        if not isinstance(files, list) or not any(
            isinstance(item, dict) and item.get("yanked") is not True for item in files
        ):
            continue
        candidates.append((tuple(int(part) for part in version.split(".")), version))
    if not candidates:
        raise RuntimeError("PyPI reports no non-yanked stable odibi-anchor release")
    return max(candidates)[1]


def _requested_project_id() -> str | None:
    explicit = globals().get("ANCHOR_PROJECT_ID")
    environment = os.environ.get("ANCHOR_PROJECT_ID")
    if explicit is not None and (not isinstance(explicit, str) or not explicit):
        raise RuntimeError("ANCHOR_PROJECT_ID init global must be a non-empty string")
    if explicit is not None and environment is not None and explicit != environment:
        raise RuntimeError("ANCHOR_PROJECT_ID init global conflicts with the environment")
    return explicit or environment

if "ANCHOR_SOURCE_CHECKOUT" in globals():
    _checkout = _validated_checkout(
        globals()["ANCHOR_SOURCE_CHECKOUT"], source="ANCHOR_SOURCE_CHECKOUT init global"
    )
elif "ANCHOR_SOURCE_CHECKOUT" in os.environ:
    _checkout = _validated_checkout(
        os.environ["ANCHOR_SOURCE_CHECKOUT"], source="ANCHOR_SOURCE_CHECKOUT environment variable"
    )
else:
    _assistant_parent = _launcher.parent.parent
    _source_candidate = _assistant_parent
    _copied_candidate = _assistant_parent / "odibi_anchor"
    try:
        _checkout = _validated_checkout(_source_candidate, source="source-tree topology")
    except RuntimeError:
        try:
            _checkout = _validated_checkout(_copied_candidate, source="copied-tree topology")
        except RuntimeError:
            _checkout = None

if _checkout is None:
    _instruction_root = _launcher.parent.parent
    _is_databricks = _launcher.as_posix().startswith("/Workspace/") or bool(
        os.environ.get("DATABRICKS_RUNTIME_VERSION")
    )
    if _is_databricks:
        _latest = _latest_stable_release()
        try:
            _installed = metadata.version("odibi-anchor")
        except metadata.PackageNotFoundError:
            _installed = None
        if _installed != _latest:
            raise RuntimeError(
                "Odibi Anchor requires the latest stable release in this Python process. Run "
                f'`%pip install "odibi-anchor[databricks]=={_latest}"`, then '
                "`dbutils.library.restartPython()` and rerun this launcher. "
                f"Resolved latest stable: {_latest}; installed: {_installed or 'missing'}."
            )

    from odibi_anchor import __version__ as _runtime_version

    try:
        _distribution_version = metadata.version("odibi-anchor")
    except metadata.PackageNotFoundError as exc:
        raise RuntimeError("odibi-anchor is not installed in this Python process") from exc
    if _distribution_version != _runtime_version:
        raise RuntimeError(
            "odibi-anchor distribution and runtime versions differ; restart Python before bootstrap"
        )

    _project_id = _requested_project_id()
    _config = globals().get(
        "ANCHOR_PORTFOLIO_CONFIG",
        os.environ.get(
            "ANCHOR_PORTFOLIO_CONFIG",
            str(_instruction_root / ".odibi-anchor" / "anchor.toml"),
        ),
    )
    _config_path = Path(os.fspath(_config))
    if _config_path.is_file():
        if _project_id is None:
            raise RuntimeError(
                "managed startup requires ANCHOR_PROJECT_ID as an init global or environment value"
            )
        from odibi_anchor import bootstrap_managed_project

        _managed = bootstrap_managed_project(
            config_path=_config_path,
            project_id=_project_id,
            instruction_root=_instruction_root,
            create_if_missing=globals().get("ANCHOR_CREATE_PROJECT") is True,
            project_root=globals().get("ANCHOR_PROJECT_ROOT"),
        )
        anchor = _managed["anchor"]
        ROOT = _managed["root"]
        MANIFEST = _managed["manifest"]
        ORIENTATION = _managed["orientation"]
        STARTUP_PACKET = _managed["startup_packet"]
        BOOTSTRAP = {
            "success": True,
            "kind": "agent_bootstrap",
            "runtime": "managed_installed_distribution",
            "repository": ROOT,
            "state_home": _managed["preparation"]["environment"]["ANCHOR_HOME"],
            "project_id": _project_id,
            "target_root": ROOT,
            "version": _runtime_version,
        }
    else:
        from odibi_anchor import launch

        _home = os.environ.get("ANCHOR_HOME")
        if not _home:
            raise RuntimeError(
                f"managed portfolio is missing at {_config_path}; installed fallback requires "
                "one explicit ANCHOR_HOME"
            )
        _target = os.environ.get("ANCHOR_PROJECT_ROOT") or str(_instruction_root)
        anchor = launch(
            anchor_home=_home,
            project_id=_project_id,
            project_root=_target,
            output_format="dict",
        )
        ROOT = str(Path(_target).resolve())
        MANIFEST = getattr(anchor, "manifest", None)
        ORIENTATION = anchor("orient", output_format="dict")
        if not isinstance(ORIENTATION, Mapping) or ORIENTATION.get("kind") != "orientation":
            raise RuntimeError("Odibi Anchor orientation returned an invalid structured result")
        _status = ORIENTATION.get("status")
        _runtime = _status.get("runtime") if isinstance(_status, Mapping) else None
        _binding = _runtime.get("route_binding") if isinstance(_runtime, Mapping) else None
        _project_id = _binding.get("project_id") if isinstance(_binding, Mapping) else None
        STARTUP_PACKET = {
            "kind": "managed_startup_packet",
            "status": "ready",
            "project_id": _project_id,
            "target_root": ROOT,
            "managed_artifact_actions": ORIENTATION.get("managed_artifact_actions", []),
            "next_required_action": (ORIENTATION.get("metrics") or {}).get(
                "next_required_action"
            ),
        }
        BOOTSTRAP = {
            "success": True,
            "kind": "agent_bootstrap",
            "runtime": "installed_distribution",
            "repository": ROOT,
            "state_home": str(Path(_home).resolve()),
            "project_id": _project_id,
            "target_root": ROOT,
            "version": _runtime_version,
        }
else:
    if globals().get("ANCHOR_CREATE_PROJECT") is True:
        raise RuntimeError("managed project creation requires installed portfolio startup")
    _delegate_globals = {}
    if "ANCHOR_REPOSITORY_PROVIDER" in globals():
        _delegate_globals["ANCHOR_REPOSITORY_PROVIDER"] = globals()["ANCHOR_REPOSITORY_PROVIDER"]
    _source_project = _requested_project_id()
    _previous_source_project = os.environ.get("ANCHOR_PROJECT_ID")
    if _source_project is not None:
        os.environ["ANCHOR_PROJECT_ID"] = _source_project
    try:
        _namespace = runpy.run_path(
            str(_checkout / "agent_bootstrap.py"),
            init_globals=_delegate_globals,
            run_name="__odibi_anchor_checkout_bootstrap__",
        )
    finally:
        if _previous_source_project is None:
            os.environ.pop("ANCHOR_PROJECT_ID", None)
        else:
            os.environ["ANCHOR_PROJECT_ID"] = _previous_source_project

    _required = ("anchor", "ROOT", "MANIFEST", "ORIENTATION", "BOOTSTRAP")
    _missing = tuple(name for name in _required if name not in _namespace)
    if _missing:
        raise RuntimeError(f"Odibi Anchor bootstrap omitted required values: {', '.join(_missing)}")
    _bootstrap = _namespace["BOOTSTRAP"]
    if not isinstance(_bootstrap, Mapping) or _bootstrap.get("success") is not True:
        raise RuntimeError("Odibi Anchor bootstrap did not report success")
    try:
        _reported_repository = Path(_bootstrap["repository"]).resolve(strict=True)
    except (KeyError, OSError, TypeError, ValueError) as exc:
        raise RuntimeError("Odibi Anchor bootstrap reported an invalid repository") from exc
    if _reported_repository != _checkout:
        raise RuntimeError(
            "Odibi Anchor bootstrap repository does not match the selected checkout: "
            f"{_reported_repository} != {_checkout}"
        )

    anchor = _namespace["anchor"]
    ROOT = _namespace["ROOT"]
    MANIFEST = _namespace["MANIFEST"]
    ORIENTATION = _namespace["ORIENTATION"]
    BOOTSTRAP = _bootstrap
    STARTUP_PACKET = _namespace.get(
        "STARTUP_PACKET",
        {
            "kind": "managed_startup_packet",
            "status": "ready",
            "project_id": BOOTSTRAP.get("project_id"),
            "target_root": ROOT,
            "managed_artifact_actions": ORIENTATION.get("managed_artifact_actions", []),
            "next_required_action": (ORIENTATION.get("metrics") or {}).get(
                "next_required_action"
            ),
        },
    )

print("ODIBI_ANCHOR_STARTUP=" + json.dumps(STARTUP_PACKET, sort_keys=True, default=str))
