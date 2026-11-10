"""Project-scoped, nonmutating artifact inspection without runtime admission.

Use :func:`inspect_artifacts` to discover saved records or read exact UTF-8 pages.
No session, database, active-project selector, or task is created or changed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


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
