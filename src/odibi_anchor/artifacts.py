"""Project-scoped, nonmutating artifact inspection without runtime admission.

Use :func:`inspect_artifacts` to discover saved records or read exact UTF-8 pages.
No session, database, active-project selector, or task is created or changed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, overload


def inspect_artifact_contracts() -> dict[str, Any]:
    """Return native writer/readiness capabilities without accessing runtime state."""
    from odibi_anchor._dispatcher._managed_record_schema import native_artifact_contracts

    return native_artifact_contracts()


@overload
def inspect_artifacts(
    *, anchor_home: str | Path, project_id: str, path: str | None = None,
    offset: int = 0, limit: int | None = None, expected_sha256: str | None = None,
    output_format: Literal["dict"] = "dict",
) -> dict[str, Any]: ...


@overload
def inspect_artifacts(
    *, anchor_home: str | Path, project_id: str, path: str | None = None,
    offset: int = 0, limit: int | None = None, expected_sha256: str | None = None,
    output_format: Literal["markdown"],
) -> str: ...


@overload
def inspect_artifacts(
    *, anchor_home: str | Path, project_id: str, path: str | None = None,
    offset: int = 0, limit: int | None = None, expected_sha256: str | None = None,
    output_format: str,
) -> dict[str, Any] | str: ...


def inspect_artifacts(
    *, anchor_home: str | Path, project_id: str, path: str | None = None,
    offset: int = 0, limit: int | None = None, expected_sha256: str | None = None,
    output_format: str = "dict",
) -> dict[str, Any] | str:
    """List a bound project's artifacts, or read ``path`` exactly, without mutation.

    Lists default to 100 entries (maximum 200). Text pages default to 32000 Unicode
    characters. Follow ``next_offset``; later text pages require the first page's
    ``content_sha256`` as ``expected_sha256``. Missing, unavailable, and stale reads
    raise instead of returning an empty record. Discovery grants no write authority.
    """
    from odibi_anchor._dispatcher._project import project_action, resolve_route_binding

    binding = resolve_route_binding(anchor_home, project=project_id, runtime_instance_id="artifact-inspection")
    values: dict[str, Any] = {"offset": offset}
    if limit is not None:
        values["limit"] = limit
    if path is not None:
        values["path"] = path
        if expected_sha256 is not None:
            values["expected_sha256"] = expected_sha256
    elif expected_sha256 is not None:
        raise ValueError("expected_sha256 is for a saved-content read, not inventory pagination")
    return project_action(anchor_home, "read_artifact" if path is not None else "artifacts",
                          route_binding=binding, output_format=output_format, **values)


def inspect_readiness(
    *, anchor_home: str | Path, project_id: str, kind: str, identifier: str,
    stage: str | None = None, rigor: str = "direct", spec: str | None = None,
) -> dict[str, Any]:
    """Inspect lifecycle readiness of saved content, without admission or mutation.

    Work items use the same version-2 evaluator as request_plan_approval with
    readiness_version=2. Problems expose framing/recommendation/close readiness.
    Specs expose plan-review structure, never existence checks for future code.
    Decisions and other convention-owned artifacts have no core approval lifecycle.
    """
    from odibi_anchor._dispatcher._project import resolve_route_binding

    binding = resolve_route_binding(anchor_home, project=project_id, runtime_instance_id="readiness-inspection")
    assert binding is not None
    root = Path(binding.artifact_root)
    if kind == "work_item":
        from odibi_anchor._dispatcher._work_item import work_item_action

        if stage not in {None, "plan"}:
            raise ValueError("work_item stage must be plan; implementation readiness belongs to workflow")
        return work_item_action(root, "check_plan", work_items=[identifier], rigor=rigor,
                                spec=spec, readiness_version=2, output_format="dict")  # type: ignore[return-value]
    if kind == "problem":
        from odibi_anchor._dispatcher._problem import problem_action

        return problem_action(root, "check_readiness", identifier, stage=stage or "framing",
                              project_id=project_id, output_format="dict")  # type: ignore[return-value]
    if kind == "spec":
        from odibi_anchor._dispatcher._spec import spec_action

        if stage not in {None, "plan"}:
            raise ValueError("spec stage must be plan; validate checks implementation files")
        return spec_action(root, "check_plan", identifier, output_format="dict")
    raise ValueError("kind must be work_item, problem, or spec; other artifacts have no core readiness lifecycle")
