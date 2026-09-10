"""Passive, non-authorizing host capability inspection.

This module deliberately cannot establish readiness. It records whether basic
local prerequisites are observable and lists the independently trusted
capabilities that remain unavailable or unqualified.
"""

from __future__ import annotations

import os
import platform
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import PROBE_SCHEMA_VERSION

_MAX_SYSTEM_FILE_BYTES = 64 * 1024
_EXTERNAL_BLOCKERS = (
    "clone3_into_cgroup_unqualified",
    "trusted_amp_inventory_unavailable",
    "gateway_revocation_unavailable",
    "host_checkout_creation_identity_unavailable",
)
_DEFAULT_PIDFD_OPEN: Callable[[int, int], int] | None = getattr(os, "pidfd_open", None)


def _read_bounded(path: Path) -> str:
    try:
        with path.open("rb") as handle:
            data = handle.read(_MAX_SYSTEM_FILE_BYTES + 1)
    except OSError as exc:
        raise ValueError(f"host capability file unavailable: {path.name}") from exc
    if len(data) > _MAX_SYSTEM_FILE_BYTES:
        raise ValueError(f"host capability file exceeds bound: {path.name}")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"host capability file is not UTF-8: {path.name}") from exc


def _has_cgroup2_mount(mountinfo: str, expected_root: Path) -> bool:
    expected = os.fspath(expected_root)
    for line in mountinfo.splitlines():
        before, separator, after = line.partition(" - ")
        fields = before.split()
        filesystem = after.split(maxsplit=1)[0] if separator and after else ""
        if len(fields) >= 5 and fields[3] == "/" and fields[4] == expected and filesystem == "cgroup2":
            return True
    return False


def _current_cgroup(raw: str) -> str:
    lines = raw.splitlines()
    if len(lines) != 1 or not lines[0].startswith("0::"):
        raise ValueError("unified process cgroup is unavailable")
    value = lines[0][3:]
    parts = value.split("/")
    if not value.startswith("/") or any(part in {".", ".."} for part in parts):
        raise ValueError("malformed unified process cgroup")
    if any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value):
        raise ValueError("malformed unified process cgroup")
    return value


def _pidfd_open_works(pidfd_open: Callable[[int, int], int] | None) -> bool:
    if pidfd_open is None:
        return False
    try:
        descriptor = pidfd_open(os.getpid(), 0)
        os.fstat(descriptor)
    except OSError:
        return False
    finally:
        if "descriptor" in locals():
            os.close(descriptor)
    return True


def _collect_host_capabilities(
    *,
    proc_root: Path,
    cgroup_root: Path,
    system: str,
    access: Callable[[Path, int], bool],
    pidfd_open: Callable[[int, int], int] | None,
) -> dict[str, Any]:
    """Collect synthetic-or-real inputs without creating an authority claim."""
    observed_system = system
    blockers: list[str] = []
    cgroup_v2 = False
    current_relative: str | None = None
    controllers: list[str] = []
    subtree_control_writable = False
    current_cgroup_writable = False
    cgroup_kill_present = False

    if observed_system != "Linux":
        blockers.append("linux_required")
    else:
        try:
            mountinfo = _read_bounded(proc_root / "self" / "mountinfo")
            cgroup_v2 = _has_cgroup2_mount(mountinfo, cgroup_root)
        except ValueError:
            cgroup_v2 = False
        if not cgroup_v2:
            blockers.append("cgroup_v2_unavailable")
        else:
            try:
                current_relative = _current_cgroup(_read_bounded(proc_root / "self" / "cgroup"))
                current = (cgroup_root / current_relative.lstrip("/")).resolve(strict=True)
                root = cgroup_root.resolve(strict=True)
                if current != root and root not in current.parents:
                    raise ValueError("current cgroup escapes cgroup root")
                controllers = sorted(set(_read_bounded(current / "cgroup.controllers").split()))
                subtree_control_writable = access(current / "cgroup.subtree_control", os.W_OK)
                current_cgroup_writable = access(current, os.W_OK)
                cgroup_kill_present = (current / "cgroup.kill").is_file()
            except (OSError, ValueError):
                blockers.append("current_cgroup_unavailable")
            else:
                if not controllers or not subtree_control_writable or not current_cgroup_writable:
                    blockers.append("delegated_cgroup_subtree_unavailable")
                if not cgroup_kill_present:
                    blockers.append("cgroup_kill_unavailable")

    pidfd_available = _pidfd_open_works(pidfd_open)
    if not pidfd_available:
        blockers.append("pidfd_open_unavailable")
    blockers.extend(_EXTERNAL_BLOCKERS)
    blockers = sorted(set(blockers))
    return {
        "document_type": "governance_host_capability_probe",
        "schema_version": PROBE_SCHEMA_VERSION,
        "outcome": "unsupported",
        "authorizes_readiness": False,
        "probe_mode": "passive_read_only",
        "platform": {"system": observed_system},
        "cgroup": {
            "version": 2 if cgroup_v2 else None,
            "current_relative_path": current_relative,
            "controllers": controllers,
            "subtree_control_writable": subtree_control_writable,
            "current_cgroup_writable": current_cgroup_writable,
            "cgroup_kill_present": cgroup_kill_present,
            "active_child_creation_qualified": False,
        },
        "process_identity": {
            "pidfd_open_available": pidfd_available,
            "clone3_into_cgroup_qualified": False,
        },
        "trusted_host_interfaces": {
            "amp_inventory": "unavailable",
            "gateway_revocation": "unavailable",
            "checkout_creation_identity": "unavailable",
        },
        "blockers": blockers,
        "limitations": [
            "Passive inspection does not create a cgroup, launch a child, kill a generation, or qualify clone3.",
            "This report is not a readiness receipt, fence qualification, or authorization input.",
            "Trusted Amp inventory, gateway revocation, and checkout identity require an external host authority.",
        ],
    }


def probe_host_capabilities() -> dict[str, Any]:
    """Inspect fixed host surfaces and return a non-authorizing passive report."""
    return _collect_host_capabilities(
        proc_root=Path("/proc"),
        cgroup_root=Path("/sys/fs/cgroup"),
        system=platform.system(),
        access=os.access,
        pidfd_open=_DEFAULT_PIDFD_OPEN,
    )
