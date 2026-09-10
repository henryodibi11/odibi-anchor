"""Bounded nonmutating runtime capability projection."""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any


def _capability(state: str, status: str, source: str, *, value: Any = None, reason: str | None = None):
    result = {"state": state, "status": status, "provenance": {"source": source}}
    if value is not None:
        result["value"] = value
    if reason is not None:
        result["reason"] = reason
    return result


def _repository_history_capability(target_root: str) -> dict[str, Any]:
    root = Path(target_root)
    try:
        head = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"], cwd=root,
            capture_output=True, timeout=10, check=False,
        )
        if head.returncode != 0:
            return _capability(
                "unavailable", "verified", "git:rev-parse-head",
                reason="the canonical repository is unborn or has no readable HEAD history",
            )
        shallow = subprocess.run(
            ["git", "rev-parse", "--is-shallow-repository"], cwd=root,
            capture_output=True, timeout=10, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return _capability(
            "unavailable", "unverified", "git:history-probe",
            reason="repository history could not be inspected",
        )
    if shallow.returncode != 0:
        return _capability(
            "unavailable", "unverified", "git:is-shallow-repository",
            reason="repository history completeness could not be inspected",
        )
    if shallow.stdout.strip() == b"true":
        return _capability(
            "incompatible", "verified", "git:is-shallow-repository",
            value={"complete": False},
            reason="the local repository is shallow and does not provide complete history",
        )
    return _capability(
        "available", "verified", "git:rev-parse-history",
        value={"complete": True},
    )


def collect_runtime_capabilities(session_state: Any, route_binding: Any | None) -> dict[str, Any]:
    """Inspect safe process/package/tool facts without installation or repair."""
    try:
        package_version = version("odibi-anchor")
        package = _capability(
            "available", "verified", "importlib.metadata:odibi-anchor",
            value={"version": package_version},
        )
    except PackageNotFoundError:
        package = _capability(
            "missing", "verified", "importlib.metadata:odibi-anchor",
            reason="installed distribution metadata was not found",
        )
    git_path = shutil.which("git")
    git = _capability(
        "available" if git_path else "missing", "verified", "shutil.which:git",
        value={"executable": "git"} if git_path else None,
        reason=None if git_path else "git executable was not found on PATH",
    )
    mcp_runtime = str(getattr(session_state, "runtime_instance_id", "") or "").startswith("mcp:")
    provider = getattr(session_state, "repository_provider", None)
    target_root = getattr(route_binding, "target_root", None) if route_binding is not None else None
    if provider is not None:
        local_git = _capability(
            "not_applicable", "not_applicable", "repository_provider",
            reason="the bound repository uses a host-attested Git Folder provider",
        )
        repository_history = _capability(
            "unavailable", "verified", "databricks_repository_capabilities:v1",
            reason="the host provider does not expose local merge-base or history evidence",
        )
    elif not git_path:
        local_git = _capability(
            "missing", "verified", "shutil.which:git",
            reason="git executable was not found on PATH",
        )
        repository_history = _capability(
            "missing", "verified", "shutil.which:git",
            reason="git executable was not found on PATH",
        )
    elif not target_root:
        local_git = _capability(
            "unverified", "unverified", "route_binding:v1",
            reason="no immutable target root is available for repository detection",
        )
        repository_history = local_git
    else:
        from odibi_anchor._repository_snapshot import canonical_local_git_available

        compatible = canonical_local_git_available(target_root)
        local_git = _capability(
            "available" if compatible else "incompatible",
            "verified", "canonical_local_git_available:v1",
            reason=None if compatible else "the exact target root is not a canonical local Git worktree",
        )
        repository_history = (
            _repository_history_capability(target_root)
            if compatible
            else _capability(
                "unavailable", "verified", "canonical_local_git_available:v1",
                reason="history is unavailable because the target is not a canonical local Git worktree",
            )
        )
    repository_surface = (
        _capability(
            "available", "verified", "repository_provider",
            value={"kind": "databricks_git_folder"},
        )
        if provider is not None
        else _capability(
            local_git["state"], local_git["status"], local_git["provenance"]["source"],
            value={"kind": "local_git"} if local_git["state"] == "available" else None,
            reason=local_git.get("reason"),
        )
    )
    return {
        "python": _capability(
            "available", "verified", "sys.version_info",
            value={"version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"},
        ),
        "odibi_anchor_package": package,
        "git": git,
        "local_git_repository": local_git,
        "repository_history": repository_history,
        "managed_route": _capability(
            "available" if route_binding is not None else "unverified",
            "verified" if route_binding is not None else "unverified",
            "route_binding:v1",
            reason=None if route_binding is not None else "no immutable RouteBinding is present",
        ),
        "direct_python": _capability("available", "verified", "python_import:v1"),
        "cli": _capability(
            "available" if importlib.util.find_spec("odibi_anchor.cli") else "missing",
            "verified", "importlib.util.find_spec:odibi_anchor.cli",
        ),
        "mcp": _capability(
            "available" if mcp_runtime else "unverified",
            "verified" if mcp_runtime else "unverified",
            "runtime_instance_id",
            reason=None if mcp_runtime else "the current process is not an attested MCP runtime",
        ),
        "repository_surface": repository_surface,
        "automatic_repair": _capability(
            "not_applicable", "not_applicable", "campaign_policy:v1",
            reason="capability detection is read-only and never repairs the environment",
        ),
    }
