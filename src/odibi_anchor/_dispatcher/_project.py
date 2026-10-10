"""Managed project workspace lifecycle for Odibi Anchor.

Projects live below ``<anchor_home>/workspace/projects`` so durable artifacts have one
portable home even when code or data targets differ between environments.
"""

from __future__ import annotations

import hashlib
import json
import ntpath
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from odibi_anchor._dispatcher._descriptor import (
    DESCRIPTOR_NAME,
    DescriptorIntegrity,
    descriptor_damaged_error,
    read_descriptor,
    render_route_update,
    require_route_roundtrip,
    validate_sha256,
    write_descriptor_atomic,
)

_ACTIVE_PROJECT_FILE = ".active_project"
_PROJECT_DESCRIPTOR = DESCRIPTOR_NAME
ROUTE_BINDING_SCHEMA_VERSION = "1.0"
_BINDING_SOURCES = frozenset({"explicit", "target_match", "legacy_selector"})
ARTIFACT_CONTRACT_VERSION = "1.2"
_ARTIFACT_CONTRACT = (
    {
        "path": "PROJECT.md",
        "root_name": "PROJECT.md",
        "scaffold": False,
        "classify_as_managed": True,
        "migratable": False,
        "managed_action": {
            "name": "project",
            "list_or_show": 'anchor("project", "status", output_format="dict")',
            "help": 'anchor("help", "project", output_format="dict")',
        },
        "use_when": "Recording stable project identity, ownership, roots, boundaries, and phase.",
        "do_not_use_for": "Session logs or implementation details.",
    },
    {
        "path": "problems/",
        "root_name": "problems",
        "scaffold": True,
        "classify_as_managed": True,
        "migratable": True,
        "migration_order": 0,
        "managed_action": {
            "name": "problem",
            "list_or_show": 'anchor("problem", "list", output_format="dict")',
            "help": 'anchor("help", "problem", output_format="dict")',
        },
        "use_when": "An observed gap or uncertainty requires investigation before choosing a response.",
        "do_not_use_for": "Disguised feature requests with a predetermined solution.",
    },
    {
        "path": "decisions/",
        "root_name": "decisions",
        "scaffold": True,
        "classify_as_managed": True,
        "migratable": True,
        "migration_order": 3,
        "managed_action": None,
        "use_when": "A consequential choice, rejected alternative, rationale, or reversal condition must survive sessions.",
        "do_not_use_for": "Routine implementation details with no durable consequence.",
    },
    {
        "path": "specs/",
        "root_name": "specs",
        "scaffold": True,
        "classify_as_managed": True,
        "migratable": True,
        "migration_order": 1,
        "managed_action": {
            "name": "spec",
            "list_or_show": 'anchor("spec", "list", output_format="dict")',
            "help": 'anchor("help", "spec", output_format="dict")',
        },
        "use_when": "A precise behavioral contract is needed because competent implementations could diverge materially.",
        "do_not_use_for": "Generic planning or work that is already unambiguous.",
    },
    {
        "path": "work_items/",
        "root_name": "work_items",
        "scaffold": True,
        "classify_as_managed": True,
        "migratable": True,
        "migration_order": 2,
        "managed_action": {
            "name": "work_item",
            "list_or_show": 'anchor("work_item", "list", output_format="dict")',
            "help": 'anchor("help", "work_item", output_format="dict")',
        },
        "use_when": "A bounded unit of work needs authorization, scope, ownership, acceptance criteria, and lifecycle state.",
        "do_not_use_for": "Unapproved ideas or unresolved investigation questions.",
    },
    {
        "path": "notebooks/",
        "root_name": "notebooks",
        "scaffold": True,
        "classify_as_managed": True,
        "migratable": False,
        "managed_action": None,
        "use_when": "Retaining reproducible analysis, execution evidence, or a durable multi-session handoff.",
        "do_not_use_for": "Canonical product source that belongs in the authorized target root.",
    },
    {
        "path": "source/",
        "root_name": "source",
        "scaffold": True,
        "classify_as_managed": True,
        "migratable": False,
        "managed_action": None,
        "use_when": "Retaining sanitized, provenance-recorded input or reference material needed by managed analysis.",
        "do_not_use_for": "Copying canonical source from a referenced target repository.",
    },
    {
        "path": "archive/",
        "root_name": "archive",
        "scaffold": True,
        "classify_as_managed": True,
        "migratable": False,
        "managed_action": None,
        "use_when": "Preserving superseded or closed-cycle material that is no longer active authority.",
        "do_not_use_for": "Records that still govern current work.",
    },
)

_PROJECT_DIRECTORIES = tuple(
    item["root_name"] for item in _ARTIFACT_CONTRACT if item["scaffold"]
)


def managed_artifact_root_names() -> frozenset[str]:
    """Return canonical managed path names plus runtime-internal artifact roots."""
    public = {
        str(item["root_name"])
        for item in _ARTIFACT_CONTRACT
        if item["classify_as_managed"]
    }
    return frozenset({*public, ".odibi-anchor", "pull_requests"})


def artifact_contract(*, artifact_root: str | None = None) -> dict[str, Any]:
    """Describe artifacts and optionally observe safe absolute discovery paths.

    Paths are point-in-time guidance, not write authority or a filesystem lock.
    Never follow a link while suggesting where an agent should write.
    """
    artifacts = [dict(item) for item in _ARTIFACT_CONTRACT]
    if artifact_root is not None:
        root = Path(artifact_root).absolute()
        for item in artifacts:
            candidate = root / item["root_name"]
            reason = None
            try:
                if any(part.is_symlink() for part in (candidate, *candidate.parents)):
                    reason = "symlink in artifact path; inspect routing before use"
                elif not root.is_dir():
                    reason = "artifact root is unavailable"
                elif candidate.exists() and (
                    candidate.is_dir() if item["path"] == "PROJECT.md" else not candidate.is_dir()
                ):
                    reason = "artifact path has an unexpected filesystem type"
            except OSError:
                reason = "artifact path observation unavailable"
            item.update(absolute_path=None if reason else str(candidate),
                        path_status="unavailable" if reason else "available", path_reason=reason)
    return {
        "version": ARTIFACT_CONTRACT_VERSION,
        "scope": "all_managed_projects",
        "source_of_truth": "odibi_anchor_runtime",
        "existing_record_rewrites_required": False,
        "path_semantics": {
            "relative_touched_base": "target_root",
            "managed_registration": "Use the artifact's available absolute_path with anchor('touched', absolute_path).",
            "authority_granted": False,
            "observation": "Refresh after rebootstrap or path replacement; discovery does not authorize edits or qualify bytes.",
        },
        "root_rule": (
            "Managed records and retained evidence belong in artifact_root; canonical product "
            "source, tests, and product documentation belong in the authorized target_root."
        ),
        "activation": (
            "A running process must rebootstrap and orient after a Odibi Anchor upgrade "
            "to receive a newer artifact contract."
        ),
        "artifacts": artifacts,
    }


def _capture_contract() -> dict[str, Any]:
    """Load the independent runtime capture contract without duplicating it here."""
    from odibi_anchor._dispatcher._capture_standards import capture_standards_contract

    return capture_standards_contract()


@dataclass(frozen=True)
class RuntimeRoots:
    """Stable logical-project identity and environment-specific runtime paths."""

    anchor_home: str
    active_project: str | None
    project_root: str | None
    artifact_root: str
    target_root: str | None
    project_type: str | None


@dataclass(frozen=True)
class RouteBinding:
    """Immutable authority that binds one runtime to one managed project route.

    ``host_thread_correlation`` is diagnostic provenance, not routing authority. It is
    omitted from the default serialized form and fingerprint so private host identifiers
    are not disclosed and cannot alter project identity.
    """

    project_id: str
    target_root: str
    artifact_root: str
    anchor_home: str
    binding_source: str
    runtime_instance_id: str
    schema_version: str = ROUTE_BINDING_SCHEMA_VERSION
    host_thread_correlation: str | None = None

    def __post_init__(self) -> None:
        """Validate and canonicalize the frozen route fields."""
        if self.schema_version != ROUTE_BINDING_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported RouteBinding schema version: {self.schema_version!r}"
            )
        if self.binding_source not in _BINDING_SOURCES:
            raise ValueError(
                "binding_source must be explicit, target_match, or legacy_selector"
            )
        runtime_id = _single_line_value(self.runtime_instance_id, "runtime_instance_id")
        correlation = self.host_thread_correlation
        if correlation is not None:
            correlation = _single_line_value(correlation, "host_thread_correlation")
        object.__setattr__(self, "project_id", _normalize_project_id(self.project_id))
        object.__setattr__(self, "target_root", _normalize_target(self.target_root))
        object.__setattr__(self, "artifact_root", _normalize_target(self.artifact_root))
        object.__setattr__(self, "anchor_home", _normalize_target(self.anchor_home))
        object.__setattr__(self, "runtime_instance_id", runtime_id)
        object.__setattr__(self, "host_thread_correlation", correlation)

    def as_dict(self, *, include_host_correlation: bool = False) -> dict[str, str]:
        """Return the stable serialized contract, redacting host correlation by default."""
        result = {
            "schema_version": self.schema_version,
            "project_id": self.project_id,
            "target_root": self.target_root,
            "artifact_root": self.artifact_root,
            "anchor_home": self.anchor_home,
            "binding_source": self.binding_source,
            "runtime_instance_id": self.runtime_instance_id,
        }
        if include_host_correlation and self.host_thread_correlation is not None:
            result["host_thread_correlation"] = self.host_thread_correlation
        return result

    def canonical_json(self) -> str:
        """Return canonical JSON used as the route identity preimage."""
        return json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))

    def fingerprint(self) -> str:
        """Return a versioned SHA-256 identity for the immutable route fields."""
        digest = hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()
        return f"sha256:{digest}"


def route_binding_diagnostics(binding: RouteBinding) -> dict[str, Any]:
    """Return safe immutable route metadata for status and host diagnostics."""
    return {
        **binding.as_dict(),
        "fingerprint": binding.fingerprint(),
        "legacy_selector_used": binding.binding_source == "legacy_selector",
        "legacy_selector_fallback_count": int(binding.binding_source == "legacy_selector"),
    }


def route_binding_staleness_reason(binding: RouteBinding) -> str | None:
    """Explain whether the bound registry record was deleted or retargeted."""
    try:
        current = resolve_active_project(binding.anchor_home, binding.project_id)
    except FileNotFoundError:
        return f"bound managed project '{binding.project_id}' no longer exists"
    except ValueError as exc:
        if getattr(exc, "error_code", None) != "managed_descriptor_damaged":
            raise
        return (
            f"bound managed project '{binding.project_id}' descriptor is damaged "
            f"({exc.context['integrity_status']})"  # type: ignore[attr-defined]
        )
    assert current is not None
    if route_path_identity(current["artifact_root"]) != route_path_identity(
        binding.artifact_root
    ):
        return f"bound managed project '{binding.project_id}' artifact root changed"
    if route_path_identity(current["target_root"]) != route_path_identity(
        binding.target_root
    ):
        return f"bound managed project '{binding.project_id}' target root changed"
    return None


def _single_line_value(value: str, field: str) -> str:
    """Validate a non-empty single-line identity value."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    normalized = value.strip()
    if "\n" in normalized or "\r" in normalized:
        raise ValueError(f"{field} must not contain line breaks")
    return normalized


def _normalize_project_id(value: str) -> str:
    """Return a safe lowercase project ID derived from a name.

    Path-like input is rejected rather than normalized so a project name can never
    escape the managed workspace.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError("project name must be a non-empty string")
    raw = value.strip()
    if "\n" in raw or "\r" in raw:
        raise ValueError("project name must not contain line breaks")
    if Path(raw).is_absolute() or ".." in raw or "/" in raw or "\\" in raw:
        raise ValueError("project name must not contain a path or '..'")
    project_id = re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-")
    if not project_id:
        raise ValueError("project name must contain at least one letter or number")
    return project_id


def _normalize_target(value: str | Path) -> str:
    """Return an absolute target reference safe for flat descriptor frontmatter."""
    raw = str(value)
    if "\n" in raw or "\r" in raw:
        raise ValueError("target must not contain line breaks")
    return str(Path(raw).resolve())


def route_path_identity(
    value: str | Path,
    *,
    platform: str | None = None,
) -> str:
    """Return a host-aware path identity for route matching.

    Windows identities are drive/case insensitive. POSIX identities resolve symlinks,
    preserve case, and deliberately keep Databricks Workspace and FUSE paths distinct.
    ``platform`` exists for deterministic cross-host contract tests.
    """
    raw = str(value)
    if not raw or "\n" in raw or "\r" in raw:
        raise ValueError("route path must be a non-empty single-line value")
    host = platform or ("windows" if os.name == "nt" else "posix")
    if host == "windows":
        normalized = ntpath.normcase(ntpath.normpath(raw.replace("/", "\\")))
        return f"windows:{normalized}"
    if host != "posix":
        raise ValueError("platform must be 'windows' or 'posix'")
    return f"posix:{Path(raw).resolve()}"


def _workspace_root(anchor_home: str | Path) -> Path:
    """Return the managed workspace root beneath a validated Odibi Anchor home."""
    return Path(anchor_home).resolve() / "workspace"


def _managed_project_root(anchor_home: str | Path, project_id: str) -> Path:
    """Return a contained managed project path."""
    projects_root = (_workspace_root(anchor_home) / "projects").resolve()
    project_root = (projects_root / _normalize_project_id(project_id)).resolve()
    if project_root.parent != projects_root:
        raise ValueError("project path escapes the managed workspace")
    return project_root


def _read_active_project_id(anchor_home: str | Path) -> str | None:
    """Read the locally remembered project ID, ignoring missing or invalid state."""
    active_path = _workspace_root(anchor_home) / _ACTIVE_PROJECT_FILE
    try:
        value = active_path.read_text(encoding="utf-8").strip()
        return _normalize_project_id(value) if value else None
    except (OSError, ValueError):
        return None


def _write_active_project_id(anchor_home: str | Path, project_id: str) -> None:
    """Persist active project selection atomically as local runtime state."""
    workspace_root = _workspace_root(anchor_home)
    workspace_root.mkdir(parents=True, exist_ok=True)
    active_path = workspace_root / _ACTIVE_PROJECT_FILE
    temporary_path = workspace_root / f"{_ACTIVE_PROJECT_FILE}.tmp"
    temporary_path.write_text(f"{_normalize_project_id(project_id)}\n", encoding="utf-8")
    temporary_path.replace(active_path)


def project_routing_fingerprint(
    anchor_home: str | Path,
    active: dict[str, str] | RouteBinding | None,
) -> tuple[tuple[int, int, int] | None, tuple[int, int, int] | None]:
    """Return a cheap version fingerprint for route authority.

    Legacy/unbound routing includes the mutable selector. An immutable ``RouteBinding``
    excludes selector state while retaining the bound project descriptor so deletion or
    retargeting still invalidates the running route.
    """
    active_path = _workspace_root(anchor_home) / _ACTIVE_PROJECT_FILE
    if isinstance(active, RouteBinding):
        selector_path = None
        descriptor_path = Path(active.artifact_root) / _PROJECT_DESCRIPTOR
    else:
        selector_path = active_path
        descriptor_path = Path(active["project_root"]) / _PROJECT_DESCRIPTOR if active else None

    def version(path: Path | None) -> tuple[int, int, int] | None:
        if path is None:
            return None
        try:
            stat = path.stat()
        except OSError:
            return None
        return stat.st_mtime_ns, stat.st_size, stat.st_ino

    return version(selector_path), version(descriptor_path)


def resolve_active_project(
    anchor_home: str | Path,
    project: str | None = None,
) -> dict[str, str] | None:
    """Resolve an explicit or remembered managed project.

    An explicit missing project is an error. A remembered selector naming a missing
    project is ignored so Odibi Anchor can still boot from its own home; a present but
    damaged descriptor always fails closed with ``managed_descriptor_damaged``.
    """
    explicit = project is not None
    project_id = _normalize_project_id(project) if explicit else _read_active_project_id(anchor_home)
    if project_id is None:
        return None
    project_root = _managed_project_root(anchor_home, project_id)
    if not _descriptor_present(project_root):
        if explicit:
            raise FileNotFoundError(
                f"Managed project '{project_id}' does not exist. "
                f"Create it with anchor('project', 'create', name='{project_id}')."
            )
        return None
    integrity = read_descriptor(project_root)
    if not integrity.intact:
        # Never substitute the artifact root for a damaged or unreadable route target.
        raise descriptor_damaged_error(
            integrity, project_id=project_id, artifact_root=str(project_root)
        )
    return {
        "project_id": project_id,
        "project_root": str(project_root),
        "artifact_root": str(project_root),
        "target_root": _descriptor_target(project_root, integrity),
        "project_type": integrity.fields["project_type"],
    }


def _descriptor_present(project_root: Path) -> bool:
    """Return descriptor presence; an unreadable directory is present and later unreadable."""
    try:
        return (project_root / _PROJECT_DESCRIPTOR).is_file()
    except PermissionError:
        return True


def _descriptor_target(project_root: Path, integrity: DescriptorIntegrity) -> str:
    """Resolve an intact descriptor target; relative targets anchor at the artifact root."""
    configured = integrity.fields["target_root"]
    if not Path(configured).is_absolute():
        return str((project_root / configured).resolve())
    return configured


def _route_target_conflict(
    active: dict[str, str], target_hint: str | Path
) -> ValueError:
    """Classify a requested-versus-descriptor target mismatch from simple disk evidence.

    ``probable_move`` means the descriptor target is absent while the requested target
    exists; every other combination is ``ambiguous``. Neither grants retarget authority.
    """
    from odibi_anchor._recovery import attach_recovery

    requested = _normalize_target(target_hint)
    descriptor_target = active["target_root"]
    descriptor_exists = Path(descriptor_target).exists()
    requested_exists = Path(requested).exists()
    classification = (
        "probable_move" if not descriptor_exists and requested_exists else "ambiguous"
    )
    explanation = (
        "The descriptor target is absent and the requested target exists, which resembles "
        "a target move."
        if classification == "probable_move"
        else "The mismatch cannot be classified safely from the observed targets."
    )
    return attach_recovery(
        ValueError(
            f"Managed project '{active['project_id']}' conflicts with target hint "
            f"(route_target_conflict, {classification}): requested target {requested!r} "
            f"differs from the intact descriptor target {descriptor_target!r}. "
            f"{explanation} No supported move-target operation exists in this version; "
            "stop and ask the project owner. Do not edit PROJECT.md, the portfolio, or "
            "Anchor state manually."
        ),
        error_code="route_target_conflict",
        context={
            "project_id": active["project_id"],
            "requested_target": requested,
            "descriptor_target": descriptor_target,
            "artifact_root": active["artifact_root"],
            "config_path": None,
            "classification": classification,
            "classification_evidence": {
                "descriptor_target_exists": descriptor_exists,
                "requested_target_exists": requested_exists,
            },
            "owner_decision_required": True,
            "supported_move_available": False,
        },
    )


def resolve_route_binding(
    anchor_home: str | Path,
    *,
    runtime_instance_id: str,
    project: str | None = None,
    target_hint: str | Path | None = None,
    allow_legacy_selector: bool = False,
    host_thread_correlation: str | None = None,
) -> RouteBinding | None:
    """Resolve one immutable runtime binding with fail-closed precedence.

    Precedence is explicit project ID, then a unique normalized target match, then the
    remembered selector only when the caller explicitly enables legacy interactive mode.
    The function never creates, selects, or retargets a managed project.
    """
    home = Path(anchor_home).resolve()
    runtime_id = _single_line_value(runtime_instance_id, "runtime_instance_id")

    def bind(active: dict[str, str], source: str) -> RouteBinding:
        return RouteBinding(
            project_id=active["project_id"],
            target_root=active["target_root"],
            artifact_root=active["artifact_root"],
            anchor_home=str(home),
            binding_source=source,
            runtime_instance_id=runtime_id,
            host_thread_correlation=host_thread_correlation,
        )

    if project is not None:
        try:
            active = resolve_active_project(home, project)
        except ValueError as exc:
            if target_hint is not None and getattr(exc, "error_code", None) == (
                "managed_descriptor_damaged"
            ):
                exc.context["requested_target"] = _normalize_target(target_hint)  # type: ignore[attr-defined]
            raise
        assert active is not None
        if target_hint is not None and route_path_identity(
            active["target_root"]
        ) != route_path_identity(target_hint):
            raise _route_target_conflict(active, target_hint)
        return bind(active, "explicit")

    if target_hint is not None:
        target_identity = route_path_identity(target_hint)
        matches: list[dict[str, str]] = []
        damaged: list[str] = []
        for item in _list_projects(home):
            if item["integrity_status"] != "intact":
                # A damaged descriptor claims no target; it can never be matched.
                damaged.append(item["id"])
                continue
            candidate = resolve_active_project(home, item["id"])
            if candidate is not None and route_path_identity(
                candidate["target_root"]
            ) == target_identity:
                matches.append(candidate)
        if not matches and damaged:
            # The intended project may be one whose route descriptor is damaged.
            damaged_root = _managed_project_root(home, damaged[0])
            error = descriptor_damaged_error(
                read_descriptor(damaged_root), project_id=damaged[0],
                artifact_root=str(damaged_root), requested_target=_normalize_target(target_hint),
            )
            error.args = (
                f"No intact managed project claims target {str(target_hint)!r}; damaged "
                f"project(s) {', '.join(damaged)} may be the intended one. {error.args[0]}",
            )
            error.context.update(  # type: ignore[attr-defined]
                damaged_project_ids=damaged,
                match_reason=(
                    "no intact project claims the requested target; a damaged project may be "
                    "the intended one"
                ),
            )
            raise error
        if not matches:
            raise FileNotFoundError(
                f"No managed project target matches {str(target_hint)!r}; "
                "provide an explicit project ID or register the target first."
            )
        if len(matches) > 1:
            project_ids = ", ".join(sorted(match["project_id"] for match in matches))
            raise ValueError(
                f"Target hint matches multiple managed projects: {project_ids}. "
                "Provide an explicit project ID; no project was selected."
            )
        return bind(matches[0], "target_match")

    selected_id = _read_active_project_id(home)
    if selected_id is None:
        return None
    if not allow_legacy_selector:
        raise RuntimeError(
            "A remembered project exists but legacy selector fallback is disabled; "
            "provide an explicit project ID or target hint."
        )
    active = resolve_active_project(home, selected_id)
    assert active is not None
    return bind(active, "legacy_selector")


def resolve_runtime_roots(
    anchor_home: str | Path,
    *,
    explicit_target: str | Path | None = None,
    project: str | None = None,
    route_binding: RouteBinding | None = None,
) -> RuntimeRoots:
    """Resolve home, logical project, artifact storage, and optional work target.

    An explicit target is an environment-local override and never changes project
    identity or the durable artifact destination.
    """
    home = Path(anchor_home).resolve()
    if route_binding is not None:
        if route_path_identity(route_binding.anchor_home) != route_path_identity(home):
            raise ValueError("RouteBinding anchor_home does not match the runtime ANCHOR_HOME")
        if project is not None and _normalize_project_id(project) != route_binding.project_id:
            raise ValueError("RouteBinding project_id conflicts with the requested project")
        if explicit_target is not None and route_path_identity(
            explicit_target
        ) != route_path_identity(route_binding.target_root):
            raise ValueError("RouteBinding target_root conflicts with the explicit target")
        return RuntimeRoots(
            anchor_home=str(home),
            active_project=route_binding.project_id,
            project_root=route_binding.artifact_root,
            artifact_root=route_binding.artifact_root,
            target_root=route_binding.target_root,
            project_type="referenced",
        )
    active = resolve_active_project(home, project)
    if active is None:
        target = _normalize_target(explicit_target) if explicit_target is not None else str(home)
        return RuntimeRoots(str(home), None, None, str(home), target, None)

    target = (
        _normalize_target(explicit_target)
        if explicit_target is not None
        else active.get("target_root")
    )
    return RuntimeRoots(
        anchor_home=str(home),
        active_project=active["project_id"],
        project_root=active["project_root"],
        artifact_root=active["artifact_root"],
        target_root=target,
        project_type=active["project_type"],
    )


def _list_projects(anchor_home: str | Path) -> list[dict[str, Any]]:
    """Return managed projects in stable ID order with read-only descriptor integrity."""
    projects_root = _workspace_root(anchor_home) / "projects"
    if not projects_root.is_dir():
        return []
    projects: list[dict[str, Any]] = []
    for project_root in sorted((path for path in projects_root.iterdir() if path.is_dir()), key=lambda p: p.name):
        if not _descriptor_present(project_root):
            continue
        try:
            integrity = read_descriptor(project_root)
        except FileNotFoundError:
            continue
        fields = integrity.fields
        projects.append({
            "id": project_root.name,
            "name": fields.get("name") or project_root.name.replace("-", " ").title(),
            "status": fields.get("status") or "active",
            "project_type": fields.get("project_type") if integrity.intact else None,
            "path": str(project_root.resolve()),
            "integrity_status": integrity.status,
            "descriptor_sha256": integrity.sha256,
            "defaulted_fields": list(integrity.defaulted_fields),
        })
    return projects


def _create_project(
    anchor_home: str | Path,
    name: str,
    *,
    target: str | Path | None = None,
) -> dict[str, Any]:
    """Create a managed project descriptor and standard artifact directories."""
    project_id = _normalize_project_id(name)
    project_root = _managed_project_root(anchor_home, project_id)
    descriptor_path = project_root / _PROJECT_DESCRIPTOR
    if project_root.exists() or project_root.is_symlink():
        raise FileExistsError(
            f"Managed project destination already exists: {project_root}. Odibi Anchor "
            "will not inspect, overwrite, or complete a partial project directory. Inspect its "
            "contents, rename it to a preserved backup path, then retry "
            f"anchor('project', 'create', name='{project_id}'). Do not manually scaffold PROJECT.md "
            "or managed subdirectories."
        )
    target_root = _normalize_target(target) if target is not None else str(project_root)
    project_type = "referenced" if target is not None else "managed"
    display_name = name.strip()
    descriptor = (
        "---\n"
        f"id: {project_id}\n"
        f"name: {display_name}\n"
        "status: active\n"
        f"project_type: {project_type}\n"
        f"target_root: {target_root}\n"
        "---\n\n"
        f"# {display_name}\n\n"
        "## Purpose\n\n"
        "[Describe what this project owns and why it exists.]\n\n"
        "## Managed paths\n\n"
        "- `source/`\n"
        "- `notebooks/`\n"
        "- `problems/`\n"
        "- `specs/`\n"
        "- `work_items/`\n"
        "- `decisions/`\n"
        "- `archive/`\n\n"
        "Artifact selection and record semantics are governed by Odibi Anchor's "
        "versioned runtime artifact contract, returned by `anchor(\"orient\")` "
        "and `anchor(\"task\", ...)`. Existing projects inherit that contract without record rewrites.\n\n"
        "## External references\n\n"
        "[Add repositories, Databricks workspaces, catalogs, dashboards, or services.]\n"
    )
    require_route_roundtrip(
        descriptor, path=str(descriptor_path), project_id=project_id, target_root=target_root,
    )
    project_root.mkdir(parents=True, exist_ok=False)
    for directory in _PROJECT_DIRECTORIES:
        (project_root / directory).mkdir(exist_ok=True)
    write_descriptor_atomic(descriptor_path, descriptor, expected_sha256=None)
    _write_active_project_id(anchor_home, project_id)
    return {
        "created": True,
        "project_id": project_id,
        "project_root": str(project_root),
        "artifact_root": str(project_root),
        "target_root": target_root,
        "project_type": project_type,
        "descriptor_path": str(descriptor_path),
    }


def _migrate_artifacts(
    project_root: Path,
    source_root: Path,
    *,
    dry_run: bool,
) -> dict[str, Any]:
    """Additively copy existing durable artifacts into a managed project."""
    planned: list[str] = []
    imported: list[str] = []
    conflicts: list[str] = []
    migratable = sorted(
        (item for item in _ARTIFACT_CONTRACT if item["migratable"]),
        key=lambda item: int(item["migration_order"]),
    )
    for category in (str(item["root_name"]) for item in migratable):
        source_directory = source_root / category
        if not source_directory.is_dir():
            continue
        for source_path in sorted(path for path in source_directory.rglob("*.md") if path.is_file()):
            relative = source_path.relative_to(source_directory)
            destination = (project_root / category / relative).resolve()
            category_root = (project_root / category).resolve()
            if destination != category_root and category_root not in destination.parents:
                raise ValueError("migration destination escapes the managed project")
            label = f"{category}/{relative.as_posix()}"
            if source_path.resolve() == destination:
                continue
            if destination.exists():
                conflicts.append(label)
                continue
            planned.append(label)
            if dry_run is False:
                destination.parent.mkdir(parents=True, exist_ok=True)
                try:
                    with destination.open("x", encoding="utf-8") as stream:
                        stream.write(source_path.read_text(encoding="utf-8"))
                except FileExistsError:
                    planned.remove(label)
                    conflicts.append(label)
                    continue
                except Exception:
                    destination.unlink(missing_ok=True)
                    raise
                imported.append(label)
    return {
        "migration": True,
        "dry_run": dry_run,
        "source_root": str(source_root),
        "planned": planned,
        "imported": imported,
        "conflicts": conflicts,
        "originals_preserved": True,
    }


def _render_project_context(result: dict[str, Any]) -> str:
    """Render a project action result as compact Markdown."""
    lines = ["# Odibi Anchor Projects", ""]
    if result.get("created"):
        lines.append(f"Created and selected **{result['project_id']}** at `{result['project_root']}`.")
        lines.append("")
    elif result.get("selected"):
        lines.append(f"Selected **{result['active_project']}**.")
        if result.get("reinitialize_required"):
            lines.append("Run `init()` again to route this dispatcher's target and artifacts to the selection.")
        lines.append("")
    elif result.get("migration"):
        mode = "Preview" if result.get("dry_run") else "Applied"
        lines.append(f"**{mode} additive artifact migration** from `{result['source_root']}`.")
        lines.append(
            f"Planned: {len(result.get('planned', []))} | "
            f"Imported: {len(result.get('imported', []))} | "
            f"Conflicts: {len(result.get('conflicts', []))}"
        )
        lines.append("Original files were preserved; conflicts were not overwritten.")
        lines.append("")
    active_project = result.get("active_project")
    lines.append(f"**Active project:** {active_project or 'none'}")
    lines.append(f"**Odibi Anchor home:** `{result['anchor_home']}`")
    if result.get("artifact_root"):
        lines.append(f"**Artifact root:** `{result['artifact_root']}`")
    if result.get("target_root"):
        lines.append(f"**Registry target root:** `{result['target_root']}`")
    if result.get("dispatcher_target_root"):
        lines.append(f"**Dispatcher target root:** `{result['dispatcher_target_root']}`")
    lines.append(f"**Routing stale:** `{bool(result.get('routing_stale'))}`")
    lines.append(f"**Reinitialize required:** `{bool(result.get('reinitialize_required'))}`")
    contract = result.get("artifact_contract") or {}
    if contract.get("version"):
        lines.append(f"**Artifact contract:** runtime v{contract['version']} (all managed projects)")
        lines.append("Relative `touched` paths always resolve against `target_root`. Use an available absolute artifact path:")
        for item in contract.get("artifacts", []):
            if "absolute_path" in item:
                location = item["absolute_path"] or item["path_reason"]
                lines.append(f"- `{item['path']}`: `{location}` ({item['path_status']})")
    capture = result.get("capture_standards") or {}
    if capture.get("version"):
        from odibi_anchor._dispatcher._capture_standards import (
            render_capture_standards_markdown,
        )

        lines.extend(["", render_capture_standards_markdown(capture)])
    lines.append("")
    projects = result.get("projects", [])
    if projects:
        lines.append("## Managed projects")
        lines.append("")
        for item in projects:
            marker = " (active)" if item["id"] == active_project else ""
            if item.get("integrity_status", "intact") != "intact":
                marker += f" (descriptor {item['integrity_status']})"
            lines.append(f"- **{item['id']}**{marker} — {item['status']} — `{item['path']}`")
    else:
        lines.append("No managed projects. Create one with `anchor('project', 'create', name='...')`.")
    if result.get("suggested_next_actions"):
        lines.extend(["", "## Next actions", ""])
        lines.extend(f"- {action}" for action in result["suggested_next_actions"])
    return "\n".join(lines)


def _remembered_project(
    anchor_home: Path,
) -> tuple[dict[str, str] | None, dict[str, Any] | None]:
    """Resolve the legacy selector, reporting descriptor damage instead of raising."""
    try:
        return resolve_active_project(anchor_home), None
    except ValueError as exc:
        if getattr(exc, "error_code", None) != "managed_descriptor_damaged":
            raise
        return None, dict(exc.context)  # type: ignore[attr-defined]


def _authority_records(database: Path, project_id: str, target_root: str) -> dict[str, list[str]]:
    """Read-only lookup of authority records whose exact owner names one target.

    Accepted tasks and workflow events store ``target_root`` in their owner identity;
    memory abandonment/recovery receipts store its SHA-256 identity. Each lookup matches
    both the raw descriptor value and the canonical form used by that record's writer.
    """
    resolved = str(Path(target_root).expanduser().resolve())
    accepted = sorted({target_root, os.path.normcase(resolved)})
    owners = sorted({target_root, resolved})
    digests = sorted({
        hashlib.sha256(value.encode("utf-8")).hexdigest() for value in (target_root, resolved)
    })

    def among(values: list[str]) -> str:
        return "(" + ",".join("?" for _ in values) + ")"

    connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        tables = {
            row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        queries = {
            "accepted_task_records": (
                "accepted_tasks",
                "SELECT task_window_id FROM accepted_task_records WHERE project_id=? "
                f"AND target_root IN {among(accepted)} ORDER BY task_window_id",
                (project_id, *accepted),
            ),
            "workflow_events": (
                "workflows",
                "SELECT DISTINCT workflow_id FROM workflow_events WHERE "
                "json_extract(event_json,'$.state.owner.project_id')=? AND "
                f"json_extract(event_json,'$.state.owner.target_root') IN {among(owners)} "
                "ORDER BY workflow_id",
                (project_id, *owners),
            ),
            "memory_selection_abandonments": (
                "memory_abandonment_receipts",
                "SELECT abandonment_id FROM memory_selection_abandonments WHERE project_id=? "
                f"AND target_identity_sha256 IN {among(digests)} ORDER BY abandonment_id",
                (project_id, *digests),
            ),
            "memory_selection_recoveries": (
                "memory_recovery_receipts",
                "SELECT recovery_id FROM memory_selection_recoveries WHERE project_id=? "
                f"AND target_identity_sha256 IN {among(digests)} ORDER BY recovery_id",
                (project_id, *digests),
            ),
        }
        found: dict[str, list[str]] = {}
        for table, (label, sql, parameters) in queries.items():
            if table in tables:
                identifiers = [row[0] for row in connection.execute(sql, parameters)]
                if identifiers:
                    found[label] = identifiers
        return found
    finally:
        connection.close()


def _retarget_invalidations(
    anchor_home: Path, project_id: str, artifact_root: Path, old_target: str, new_target: str,
) -> dict[str, Any]:
    """Name continuity and authority records that a retarget would orphan."""
    invalidated: dict[str, Any] = {}
    sentinel = artifact_root / "continuity" / "v1" / "OWNER.json"
    if sentinel.exists() or sentinel.is_symlink():
        try:
            owner_target = json.loads(sentinel.read_text(encoding="utf-8")).get("target_root")
            owner_matches = route_path_identity(owner_target) == route_path_identity(new_target)
        except (OSError, ValueError, AttributeError, TypeError):
            # An unreadable or malformed sentinel cannot prove the retarget is safe.
            owner_target, owner_matches = None, False
        if not owner_matches:
            invalidated["continuity_owner"] = {"path": str(sentinel), "target_root": owner_target}
    from odibi_anchor.codebase._memory_db import _resolve_default_db_path

    databases: list[Path] = []
    for candidate in (Path(_resolve_default_db_path()), anchor_home / ".agent_memory.db"):
        resolved = candidate.resolve()
        if resolved not in databases and resolved.is_file():
            databases.append(resolved)
    for database in databases:
        try:
            records = _authority_records(database, project_id, old_target)
        except (OSError, sqlite3.Error) as exc:
            invalidated.setdefault("authority_unavailable", []).append(
                {"database": str(database), "error_type": type(exc).__name__}
            )
            continue
        for label, identifiers in records.items():
            entry = invalidated.setdefault(label, {"count": 0, "ids": [], "databases": []})
            entry["count"] += len(identifiers)
            entry["ids"] = (entry["ids"] + identifiers)[:20]
            entry["databases"].append(str(database))
    return invalidated


def _set_target(
    anchor_home: Path, project_id: str, target_root: str, *, expected_sha256: str | None,
) -> dict[str, Any]:
    """Retarget one never-launched project by atomically rewriting only its route lines."""
    from odibi_anchor._recovery import attach_recovery, dispatcher_operation

    project_root = _managed_project_root(anchor_home, project_id)
    descriptor_path = project_root / _PROJECT_DESCRIPTOR
    if not descriptor_path.is_file():
        resolve_active_project(anchor_home, project_id)  # Raises the standard missing-project error.
    integrity = read_descriptor(project_root)
    if not integrity.intact:
        raise descriptor_damaged_error(
            integrity, project_id=project_id, artifact_root=str(project_root),
            requested_target=target_root,
        )
    current_target = _descriptor_target(project_root, integrity)
    status_operation = dispatcher_operation(
        "project", "status", kwargs={"output_format": "dict"},
        reason="read the current descriptor_sha256 and route before asking the owner how to proceed",
    )

    def changed(actual: str | None) -> ValueError:
        return attach_recovery(
            ValueError(
                f"Managed project '{project_id}' descriptor changed (managed_descriptor_changed): "
                f"expected sha256 {expected_sha256 or integrity.sha256}, found {actual or 'absent'}. "
                "The descriptor was not changed; re-read it before deciding whether to retry."
            ),
            error_code="managed_descriptor_changed",
            context={
                "project_id": project_id,
                "descriptor_path": str(descriptor_path),
                "expected_sha256": expected_sha256 or integrity.sha256,
                "actual_sha256": actual,
                "requested_target": target_root,
            },
            next_operations=[status_operation],
        )

    if expected_sha256 is not None and expected_sha256 != integrity.sha256:
        raise changed(integrity.sha256)
    target_changed = route_path_identity(current_target) != route_path_identity(target_root)
    if target_changed:
        invalidated = _retarget_invalidations(
            anchor_home, project_id, project_root, current_target, target_root
        )
        if invalidated:
            raise attach_recovery(
                ValueError(
                    f"Managed project '{project_id}' cannot be retargeted from "
                    f"{current_target!r} to {target_root!r} "
                    "(project_retarget_requires_migration): records bound to the current "
                    "target would be invalidated or could not be verified: "
                    f"{', '.join(sorted(invalidated))}. The descriptor was not changed. No "
                    "supported retarget migration exists in this version; stop and ask the "
                    "project owner. Do not edit PROJECT.md, continuity files, or SQLite manually."
                ),
                error_code="project_retarget_requires_migration",
                context={
                    "project_id": project_id,
                    "descriptor_path": str(descriptor_path),
                    "descriptor_sha256": integrity.sha256,
                    "artifact_root": str(project_root),
                    "current_target": current_target,
                    "requested_target": target_root,
                    "invalidated": invalidated,
                    "owner_decision_required": True,
                    "supported_migration_available": False,
                },
            )
    updated = render_route_update(
        integrity, {"project_type": "referenced", "target_root": target_root}
    )
    require_route_roundtrip(
        updated, path=str(descriptor_path), project_id=project_id, target_root=target_root,
    )
    descriptor_sha256 = integrity.sha256
    if updated != integrity.text:
        try:
            descriptor_sha256 = write_descriptor_atomic(
                descriptor_path, updated, expected_sha256=integrity.sha256
            )
        except FileExistsError as exc:
            actual = (
                hashlib.sha256(descriptor_path.read_bytes()).hexdigest()
                if descriptor_path.is_file() else None
            )
            raise changed(actual) from exc
    return {
        "target_updated": updated != integrity.text,
        "project_root": str(project_root),
        "artifact_root": str(project_root),
        "target_root": target_root,
        "previous_target_root": current_target,
        "project_type": "referenced",
        "descriptor_path": str(descriptor_path),
        "previous_descriptor_sha256": integrity.sha256,
        "descriptor_sha256": descriptor_sha256,
    }


def project_action(
    anchor_home: str | Path,
    *args: Any,
    name: str | None = None,
    target: str | Path | None = None,
    current_project: str | None = None,
    current_target: str | None = None,
    route_binding: RouteBinding | None = None,
    routing_stale: bool = False,
    dry_run: bool = True,
    expected_sha256: str | None = None,
    output_format: str = "markdown",
    **_extra: Any,
) -> dict[str, Any] | str:
    """Create, list, select, or inspect Odibi Anchor managed projects."""
    if output_format not in {"dict", "markdown"}:
        raise ValueError("output_format must be 'dict' or 'markdown'")
    anchor_home_path = Path(anchor_home).resolve()
    if not anchor_home_path.is_dir():
        raise ValueError(f"anchor_home must be an existing directory: {anchor_home_path}")

    command = str(args[0]).lower().strip() if args else "list"
    command_arg = str(args[1]) if len(args) > 1 else None
    active, active_damage = _remembered_project(anchor_home_path)
    result: dict[str, Any] = {
        "kind": "project_context",
        "anchor_home": str(anchor_home_path),
        "workspace_root": str(_workspace_root(anchor_home_path)),
        "active_project": active["project_id"] if active else None,
        "dispatcher_project": current_project,
        "dispatcher_target_root": current_target,
        "routing_stale": routing_stale,
        "projects": [],
        "artifact_contract": artifact_contract(),
        "capture_standards": _capture_contract(),
        "suggested_next_actions": [],
    }
    if route_binding is not None:
        result["route_binding"] = route_binding_diagnostics(route_binding)

    if command in {"list", "status", ""}:
        pass
    elif command == "create":
        created = _create_project(anchor_home_path, command_arg or name or "", target=target)
        result.update(created)
        result["active_project"] = created["project_id"]
        result["reinitialize_required"] = (
            route_binding is None and current_project != created["project_id"]
        )
    elif command == "use":
        project_id = _normalize_project_id(command_arg or name or "")
        resolved = resolve_active_project(anchor_home_path, project_id)
        assert resolved is not None
        if active is None or active["project_id"] != project_id:
            _write_active_project_id(anchor_home_path, project_id)
        result.update({
            "selected": True,
            "active_project": project_id,
            "project_root": resolved["project_root"],
            "artifact_root": resolved["artifact_root"],
            "target_root": resolved["target_root"],
            "reinitialize_required": route_binding is None and current_project != project_id,
        })
        if route_binding is not None:
            result["runtime_binding_unchanged"] = True
    elif command == "set_target":
        project_id = _normalize_project_id(command_arg or name or "")
        if target is None:
            raise ValueError("set_target requires target=...")
        if expected_sha256 is not None:
            validate_sha256(expected_sha256, "expected_sha256")
        result.update(_set_target(
            anchor_home_path, project_id, _normalize_target(target),
            expected_sha256=expected_sha256,
        ))
        result["reinitialize_required"] = result["target_updated"] and (
            route_binding.project_id == project_id
            if route_binding is not None
            else current_project == project_id and current_target != result["target_root"]
        )
    elif command == "migrate":
        if type(dry_run) is not bool:
            raise ValueError("dry_run must be true or false")
        project_id = _normalize_project_id(command_arg or name or result["active_project"] or "")
        resolved = resolve_active_project(anchor_home_path, project_id)
        assert resolved is not None
        source = target
        if source is None and project_id == current_project:
            source = current_target
        source_root = Path(source or resolved["target_root"]).resolve()
        if not source_root.is_dir():
            raise ValueError(f"migration source must be an existing directory: {source_root}")
        result.update(
            _migrate_artifacts(Path(resolved["project_root"]), source_root, dry_run=dry_run)
        )
        result.update({
            "project_root": resolved["project_root"],
            "artifact_root": resolved["artifact_root"],
            "target_root": str(source_root),
            "project_type": resolved["project_type"],
        })
    else:
        raise ValueError("Unknown project sub-command. Valid: list, create, use, status, set_target, migrate")

    active, active_damage = _remembered_project(anchor_home_path)
    if active_damage is not None:
        # Read-only status reports the remembered project's damage instead of crashing.
        result["active_project"] = active_damage["project_id"]
        result["active_project_descriptor_damage"] = active_damage
    if active:
        result.setdefault("project_root", active["project_root"])
        result.setdefault("artifact_root", active["artifact_root"])
        result.setdefault("target_root", active["target_root"])
        result.setdefault("project_type", active["project_type"])
        if command in {"list", "status", ""}:
            result["reinitialize_required"] = routing_stale if route_binding is not None else (
                current_project != active["project_id"] or current_target != active["target_root"]
            )
    result["projects"] = _list_projects(anchor_home_path)
    result["suggested_next_actions"] = (
        ["Re-run init() to activate the selected project's routing."]
        if result.get("reinitialize_required")
        else ["Create or resume project work with anchor('task') after orientation."]
    )
    if active_damage is not None:
        result["suggested_next_actions"].insert(0, (
            f"Stop and ask the project owner: the remembered project "
            f"'{active_damage['project_id']}' has a damaged PROJECT.md route descriptor "
            f"({active_damage['integrity_status']}); no supported repair exists in this version."
        ))
    # Discovery follows the immutable runtime binding, never a changed legacy selector.
    discovery_root = route_binding.artifact_root if route_binding is not None else result.get("artifact_root")
    result["artifact_contract"] = artifact_contract(
        artifact_root=None if routing_stale else discovery_root,
    )
    if output_format == "dict":
        return result
    return _render_project_context(result)
