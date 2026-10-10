"""Gate protection of managed ``PROJECT.md`` route fields.

When a task's changed set includes the bound project's ``PROJECT.md``, the gate
re-reads it and compares its route fields with the task's accepted route authority:
the bound project ID, target root and artifact root recorded when the task was
accepted. A runtime can only accept a task under an intact descriptor, so that
authority is the task's pre-edit route baseline, and it survives restarts. The
descriptor's ``project_type`` is not part of that authority, so it is checked for
consistency with the bound route: ``managed`` must target its own artifact root.

Body-only edits pass. Route changes and damage go through supported operations
(``project move-target`` for a target change, ``project repair-descriptor`` for
damage), both of which require the project owner.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from odibi_anchor._dispatcher._descriptor import DESCRIPTOR_NAME, read_descriptor

SUPPORTED_OPERATIONS = {
    "project move-target": "move the project's target with owner approval (route field change)",
    "project repair-descriptor": (
        "owner-approved, portfolio-authorized repair of damaged route frontmatter"
    ),
}


def _real(path: str, root: str) -> str:
    return os.path.realpath(path if os.path.isabs(path) else os.path.join(root, path))


def check_descriptor_route_protection(session_state: Any, *, changed_paths: Iterable[str]) -> None:
    """Raise ``managed_descriptor_route_change`` when a changed PROJECT.md moved its route."""
    project_id = getattr(session_state, "active_project", None)
    artifact_root = getattr(session_state, "artifact_root", None)
    target_root = getattr(session_state, "target_root", None)
    if not (project_id and artifact_root and target_root):
        return
    descriptor = os.path.realpath(os.path.join(artifact_root, DESCRIPTOR_NAME))
    ledger = [entry.path for entry in getattr(session_state, "managed_artifact_ledger", ())]
    if not any(_real(path, target_root) == descriptor for path in [*changed_paths, *ledger]):
        return

    from odibi_anchor._dispatcher._project import route_path_identity
    from odibi_anchor._recovery import attach_recovery, dispatcher_operation

    try:
        integrity = read_descriptor(artifact_root)
    except FileNotFoundError:
        integrity = None
    expected = {"id": project_id, "target_root": target_root}
    changed: list[str] = []
    observed: dict[str, str] = {}
    if integrity is None or not integrity.intact:
        changed = ["frontmatter"]
    else:
        observed = {key: integrity.fields[key] for key in ("id", "project_type", "target_root")}
        configured = observed["target_root"]
        resolved = configured if Path(configured).is_absolute() else str(
            (Path(artifact_root) / configured).resolve()
        )
        target_identity = route_path_identity(resolved)
        # An unbound legacy runtime may run with an environment-local target override,
        # so only a RouteBinding makes the target part of the accepted route authority.
        bound = getattr(session_state, "route_fingerprint", None) is not None
        if observed["id"] != project_id:
            changed.append("id")
        if bound and target_identity != route_path_identity(target_root):
            changed.append("target_root")
        if bound and observed["project_type"] == "managed" and target_identity != (
            route_path_identity(artifact_root)
        ):
            changed.append("project_type")
    if not changed:
        return
    status = "absent" if integrity is None else integrity.status
    raise attach_recovery(
        RuntimeError(
            f"BLOCKED: anchor(\"gate\") — PROJECT.md route fields changed in this task "
            f"(managed_descriptor_route_change): {', '.join(changed)} no longer match the "
            f"accepted route of '{project_id}' (descriptor {status}). Route changes are not "
            "delivered through task edits; only body edits are. Restore the accepted route "
            "fields, or stop and ask the project owner to use `project move-target` (target "
            "change) or `project repair-descriptor` (damage)."
        ),
        error_code="managed_descriptor_route_change",
        context={
            "project_id": project_id,
            "descriptor_path": descriptor,
            "descriptor_sha256": None if integrity is None else integrity.sha256,
            "integrity_status": status,
            "detail": None if integrity is None else integrity.detail,
            "changed_fields": changed,
            "expected": {**expected, "artifact_root": artifact_root},
            "observed": observed,
            "supported_operations": SUPPORTED_OPERATIONS,
            "owner_decision_required": True,
        },
        next_operations=[
            dispatcher_operation(
                "project", "status", kwargs={"output_format": "dict"},
                reason="read the current route and descriptor_sha256 before restoring the "
                "accepted route fields or asking the owner",
            ),
        ],
    )
