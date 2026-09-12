"""Tests for the self-contained Odibi Anchor managed project workspace."""

from pathlib import Path

import pytest

from odibi_anchor._dispatcher._effects import (
    build_static_action_contracts,
    enforce_effects,
    resolve_invocation,
)
from odibi_anchor._dispatcher._project import (
    ROUTE_BINDING_SCHEMA_VERSION,
    RouteBinding,
    _normalize_project_id,
    artifact_contract,
    project_action,
    project_routing_fingerprint,
    resolve_active_project,
    resolve_route_binding,
    resolve_runtime_roots,
    route_binding_diagnostics,
    route_binding_staleness_reason,
    route_path_identity,
)


def test_normalize_project_id() -> None:
    assert _normalize_project_id("Queue Automation") == "queue-automation"
    assert _normalize_project_id("bronze_customer") == "bronze-customer"


@pytest.mark.parametrize("value", ["../escape", "a/b", "a\\b", "C:\\absolute", "..."])
def test_project_id_rejects_paths(value: str) -> None:
    with pytest.raises(ValueError):
        _normalize_project_id(value)


def test_project_descriptor_values_reject_line_breaks(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="line breaks"):
        project_action(tmp_path, "create", name="alpha\nstatus: archived", output_format="dict")

    with pytest.raises(ValueError, match="line breaks"):
        project_action(
            tmp_path,
            "create",
            name="alpha",
            target="external\nstatus: archived",
            output_format="dict",
        )


def test_create_builds_managed_layout_and_selects_project(tmp_path: Path) -> None:
    result = project_action(
        tmp_path,
        "create",
        name="Queue Automation",
        current_project=None,
        output_format="dict",
    )

    project_root = tmp_path / "workspace" / "projects" / "queue-automation"
    assert result["created"] is True
    assert result["active_project"] == "queue-automation"
    assert (project_root / "PROJECT.md").is_file()
    for directory in ("source", "notebooks", "problems", "specs", "work_items", "decisions", "archive"):
        assert (project_root / directory).is_dir()
    descriptor = (project_root / "PROJECT.md").read_text(encoding="utf-8")
    assert "versioned runtime artifact contract" in descriptor
    assert "Existing projects inherit that contract without record rewrites." in descriptor
    assert (tmp_path / "workspace" / ".active_project").read_text(encoding="utf-8") == "queue-automation\n"


def test_artifact_contract_is_runtime_owned_and_complete() -> None:
    contract = artifact_contract()

    assert contract["scope"] == "all_managed_projects"
    assert contract["source_of_truth"] == "odibi_anchor_runtime"
    assert contract["existing_record_rewrites_required"] is False
    assert "rebootstrap and orient" in contract["activation"]
    assert tuple(item["path"] for item in contract["artifacts"]) == (
        "PROJECT.md", "problems/", "decisions/", "specs/", "work_items/",
        "notebooks/", "source/", "archive/",
    )
    assert all("root_name" in item for item in contract["artifacts"])
    assert all(item["use_when"] and item["do_not_use_for"] for item in contract["artifacts"])
    actions = {
        item["path"]: item["managed_action"] for item in contract["artifacts"]
    }
    assert actions["work_items/"]["list_or_show"] == (
        'anchor("work_item", "list", output_format="dict")'
    )
    assert actions["notebooks/"] is None


def test_existing_project_inherits_runtime_contract_without_record_rewrite(tmp_path: Path) -> None:
    project_action(tmp_path, "create", name="Existing", output_format="dict")
    descriptor = tmp_path / "workspace" / "projects" / "existing" / "PROJECT.md"
    descriptor.write_text(
        descriptor.read_text(encoding="utf-8") + "\n## Owner notes\n\nPreserve exactly.\n",
        encoding="utf-8",
    )
    before = descriptor.read_bytes()

    status = project_action(tmp_path, "status", output_format="dict")

    assert status["artifact_contract"] == artifact_contract()
    assert status["capture_standards"]["version"] == "1.1"
    assert status["capture_standards"]["scope"] == "all_tasks_and_managed_projects"
    assert descriptor.read_bytes() == before


def test_project_markdown_exposes_capture_standard(tmp_path: Path) -> None:
    project_action(tmp_path, "create", name="Visible", output_format="dict")

    status = project_action(tmp_path, "status", output_format="markdown")

    assert "## Capture standards v1.1" in status
    assert "**Common fields:**" in status
    assert "Do not use for:" in status
    assert "### Offline references" in status


def test_create_does_not_overwrite_existing_project(tmp_path: Path) -> None:
    project_action(tmp_path, "create", name="alpha", output_format="dict")
    descriptor = tmp_path / "workspace" / "projects" / "alpha" / "PROJECT.md"
    original = descriptor.read_text(encoding="utf-8")
    external = tmp_path / "existing-external-target"
    external.mkdir()
    owner_file = external / "owner-work.bin"
    owner_file.write_bytes(b"unknown external bytes\x00\xff")

    with pytest.raises(FileExistsError):
        project_action(
            tmp_path,
            "create",
            name="alpha",
            target=external,
            output_format="dict",
        )

    assert descriptor.read_text(encoding="utf-8") == original
    assert owner_file.read_bytes() == b"unknown external bytes\x00\xff"


@pytest.mark.parametrize("with_unknown_content", [False, True])
def test_create_safe_stops_without_inspecting_or_completing_partial_directory(
    tmp_path: Path,
    with_unknown_content: bool,
) -> None:
    partial = tmp_path / "workspace" / "projects" / "ice-loss-detection"
    partial.mkdir(parents=True)
    unknown = partial / "owner-work.txt"
    if with_unknown_content:
        unknown.write_bytes(b"preserve these exact bytes\x00\xff")
    before = tuple(
        (path.relative_to(partial).as_posix(), path.read_bytes())
        for path in partial.rglob("*")
        if path.is_file()
    )

    with pytest.raises(FileExistsError) as raised:
        project_action(
            tmp_path,
            "create",
            name="Ice Loss Detection",
            output_format="dict",
        )

    message = str(raised.value)
    assert "will not inspect, overwrite, or complete" in message
    assert "rename it to a preserved backup path" in message
    assert "Do not manually scaffold PROJECT.md" in message
    assert not (partial / "PROJECT.md").exists()
    assert not (tmp_path / "workspace" / ".active_project").exists()
    after = tuple(
        (path.relative_to(partial).as_posix(), path.read_bytes())
        for path in partial.rglob("*")
        if path.is_file()
    )
    assert after == before


def test_list_and_status_report_active_project(tmp_path: Path) -> None:
    project_action(tmp_path, "create", name="Beta Project", output_format="dict")
    project_action(tmp_path, "create", name="Alpha Project", output_format="dict")

    result = project_action(tmp_path, output_format="dict")

    assert result["active_project"] == "alpha-project"
    assert [project["id"] for project in result["projects"]] == ["alpha-project", "beta-project"]
    assert result["project_root"].endswith("alpha-project")


def test_use_persists_selection_and_requests_reinitialization(tmp_path: Path) -> None:
    alpha_target = tmp_path / "alpha-target"
    beta_target = tmp_path / "beta-target"
    project_action(tmp_path, "create", name="alpha", target=alpha_target, output_format="dict")
    project_action(tmp_path, "create", name="beta", target=beta_target, output_format="dict")

    result = project_action(
        tmp_path,
        "use",
        "alpha",
        current_project="beta",
        output_format="dict",
    )

    assert result["selected"] is True
    assert result["active_project"] == "alpha"
    assert result["target_root"] == str(alpha_target.resolve())
    assert result["reinitialize_required"] is True
    assert resolve_active_project(tmp_path) == {
        "project_id": "alpha",
        "project_root": str((tmp_path / "workspace" / "projects" / "alpha").resolve()),
        "artifact_root": str((tmp_path / "workspace" / "projects" / "alpha").resolve()),
        "target_root": str(alpha_target.resolve()),
        "project_type": "referenced",
    }


def test_use_exact_remembered_project_is_safe_orientation_without_writes(
    tmp_path: Path,
) -> None:
    project_action(tmp_path, "create", name="Alpha Project", output_format="dict")
    project_action(tmp_path, "create", name="Beta Project", output_format="dict")
    project_action(tmp_path, "use", "alpha-project", output_format="dict")
    before = {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    contract = build_static_action_contracts(anchor_home=tmp_path)["project"]

    resolution = resolve_invocation(
        contract,
        ("use", "Alpha Project"),
        {"output_format": "dict"},
    )
    enforce_effects("project", resolution.effects, None)
    result = project_action(
        tmp_path,
        "use",
        "Alpha Project",
        current_project="beta-project",
        output_format="dict",
    )

    after = {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    assert (resolution.effects, resolution.pre_task_access, resolution.error) == (
        ("read",),
        "safe_orientation",
        None,
    )
    assert isinstance(result, dict)
    assert result["active_project"] == "alpha-project"
    assert result["reinitialize_required"] is True
    assert before == after


def test_referenced_project_keeps_artifacts_managed(tmp_path: Path) -> None:
    external = tmp_path / "external-repository"
    external.mkdir()
    owner_file = external / "source.py"
    owner_file.write_bytes(b"print('owner source')\r\n")
    before = {
        path.relative_to(external).as_posix(): path.read_bytes()
        for path in external.rglob("*")
        if path.is_file()
    }

    created = project_action(
        tmp_path,
        "create",
        name="External Queue",
        target=external,
        output_format="dict",
    )
    roots = resolve_runtime_roots(tmp_path)

    assert created["project_type"] == "referenced"
    assert roots.active_project == "external-queue"
    assert roots.artifact_root == str(
        (tmp_path / "workspace" / "projects" / "external-queue").resolve()
    )
    assert roots.target_root == str(external.resolve())
    assert Path(roots.artifact_root) != external.resolve()
    assert {
        path.relative_to(external).as_posix(): path.read_bytes()
        for path in external.rglob("*")
        if path.is_file()
    } == before


def test_relative_referenced_target_resolves_from_managed_project(tmp_path: Path) -> None:
    project_root = tmp_path / "workspace" / "projects" / "portable-project"
    project_action(tmp_path, "create", name="Portable Project", output_format="dict")
    descriptor = project_root / "PROJECT.md"
    descriptor.write_text(
        descriptor.read_text(encoding="utf-8").replace(
            f"target_root: {project_root}",
            "target_root: ../../..",
        ),
        encoding="utf-8",
    )

    roots = resolve_runtime_roots(tmp_path)

    assert roots.active_project == "portable-project"
    assert roots.artifact_root == str(project_root.resolve())
    assert roots.target_root == str(tmp_path.resolve())


def test_explicit_target_overrides_location_not_project_identity(tmp_path: Path) -> None:
    first_target = tmp_path / "databricks"
    second_target = tmp_path / "local-clone"
    project_action(
        tmp_path,
        "create",
        name="Portable Project",
        target=first_target,
        output_format="dict",
    )

    roots = resolve_runtime_roots(tmp_path, explicit_target=second_target)

    assert roots.active_project == "portable-project"
    assert roots.target_root == str(second_target.resolve())
    assert Path(roots.artifact_root) == (
        tmp_path / "workspace" / "projects" / "portable-project"
    ).resolve()


def test_set_target_updates_reference_without_moving_artifacts(tmp_path: Path) -> None:
    project_action(tmp_path, "create", name="alpha", output_format="dict")
    target = tmp_path / "external"

    result = project_action(
        tmp_path,
        "set_target",
        "alpha",
        target=target,
        current_project="alpha",
        current_target=str(tmp_path),
        output_format="dict",
    )

    assert result["target_updated"] is True
    assert result["target_root"] == str(target.resolve())
    assert result["project_type"] == "referenced"
    assert Path(result["artifact_root"]) == (
        tmp_path / "workspace" / "projects" / "alpha"
    ).resolve()


def test_status_reports_registry_target_not_stale_dispatcher_target(tmp_path: Path) -> None:
    target = tmp_path / "external"
    project_action(tmp_path, "create", name="alpha", target=target, output_format="dict")

    result = project_action(
        tmp_path,
        "status",
        current_project="alpha",
        current_target=str(tmp_path / "stale-target"),
        output_format="dict",
    )

    assert result["target_root"] == str(target.resolve())
    assert result["dispatcher_target_root"] == str(tmp_path / "stale-target")
    assert result["reinitialize_required"] is True


def test_migrate_artifacts_is_additive_and_reports_conflicts(tmp_path: Path) -> None:
    source = tmp_path / "legacy-project"
    (source / "specs").mkdir(parents=True)
    (source / "decisions").mkdir()
    (source / "specs" / "FEATURE_SPEC.md").write_text("# Feature\n", encoding="utf-8")
    (source / "decisions" / "ADR-1.md").write_text("# Decision\n", encoding="utf-8")
    project_action(tmp_path, "create", name="alpha", target=source, output_format="dict")

    preview = project_action(tmp_path, "migrate", "alpha", dry_run=True, output_format="dict")
    applied = project_action(tmp_path, "migrate", "alpha", dry_run=False, output_format="dict")
    repeated = project_action(tmp_path, "migrate", "alpha", dry_run=False, output_format="dict")

    project_root = tmp_path / "workspace" / "projects" / "alpha"
    assert preview["planned"] == ["specs/FEATURE_SPEC.md", "decisions/ADR-1.md"]
    assert preview["imported"] == []
    assert applied["imported"] == ["specs/FEATURE_SPEC.md", "decisions/ADR-1.md"]
    assert repeated["conflicts"] == ["specs/FEATURE_SPEC.md", "decisions/ADR-1.md"]
    assert (project_root / "specs" / "FEATURE_SPEC.md").is_file()
    assert (source / "specs" / "FEATURE_SPEC.md").is_file()
    assert applied["originals_preserved"] is True


def test_migrate_named_project_uses_its_target_not_active_target(tmp_path: Path) -> None:
    alpha_source = tmp_path / "alpha-source"
    beta_source = tmp_path / "beta-source"
    (alpha_source / "specs").mkdir(parents=True)
    (beta_source / "specs").mkdir(parents=True)
    (alpha_source / "specs" / "ALPHA.md").write_text("# Alpha\n", encoding="utf-8")
    (beta_source / "specs" / "BETA.md").write_text("# Beta\n", encoding="utf-8")
    project_action(tmp_path, "create", name="alpha", target=alpha_source, output_format="dict")
    project_action(tmp_path, "create", name="beta", target=beta_source, output_format="dict")

    result = project_action(
        tmp_path,
        "migrate",
        "alpha",
        current_project="beta",
        current_target=str(beta_source),
        dry_run=False,
        output_format="dict",
    )

    assert result["imported"] == ["specs/ALPHA.md"]
    assert not (tmp_path / "workspace" / "projects" / "alpha" / "specs" / "BETA.md").exists()


def test_migrate_requires_boolean_dry_run_confirmation(tmp_path: Path) -> None:
    source = tmp_path / "legacy"
    source.mkdir()
    project_action(tmp_path, "create", name="alpha", target=source, output_format="dict")

    with pytest.raises(ValueError, match="dry_run must be true or false"):
        project_action(tmp_path, "migrate", "alpha", dry_run=None, output_format="dict")


def test_explicit_missing_project_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="does not exist"):
        resolve_active_project(tmp_path, "missing")


def test_stale_remembered_project_falls_back_cleanly(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".active_project").write_text("missing\n", encoding="utf-8")

    assert resolve_active_project(tmp_path) is None
    assert project_action(tmp_path, output_format="dict")["active_project"] is None


def test_markdown_output_is_human_readable(tmp_path: Path) -> None:
    result = project_action(tmp_path, "create", name="Alpha", output_format="markdown")
    assert "# Odibi Anchor Projects" in result
    assert "Created and selected **alpha**" in result


def test_route_binding_is_immutable_versioned_and_canonical(tmp_path: Path) -> None:
    project_action(tmp_path, "create", name="Alpha", target=tmp_path / "target", output_format="dict")

    binding = resolve_route_binding(
        tmp_path,
        project="alpha",
        runtime_instance_id="runtime-a",
        host_thread_correlation="thread-private",
    )

    assert isinstance(binding, RouteBinding)
    assert binding.schema_version == ROUTE_BINDING_SCHEMA_VERSION
    assert binding.binding_source == "explicit"
    assert binding.as_dict() == {
        "schema_version": ROUTE_BINDING_SCHEMA_VERSION,
        "project_id": "alpha",
        "target_root": str((tmp_path / "target").resolve()),
        "artifact_root": str((tmp_path / "workspace" / "projects" / "alpha").resolve()),
        "anchor_home": str(tmp_path.resolve()),
        "binding_source": "explicit",
        "runtime_instance_id": "runtime-a",
    }
    assert binding.as_dict(include_host_correlation=True)["host_thread_correlation"] == "thread-private"
    assert binding.canonical_json() == RouteBinding(**binding.as_dict()).canonical_json()
    assert binding.fingerprint().startswith("sha256:")
    with pytest.raises(AttributeError):
        binding.project_id = "beta"  # type: ignore[misc]


def test_resolve_route_binding_precedence_and_conflict(tmp_path: Path) -> None:
    alpha_target = tmp_path / "target-alpha"
    beta_target = tmp_path / "target-beta"
    project_action(tmp_path, "create", name="alpha", target=alpha_target, output_format="dict")
    project_action(tmp_path, "create", name="beta", target=beta_target, output_format="dict")

    explicit = resolve_route_binding(
        tmp_path,
        project="alpha",
        target_hint=alpha_target,
        runtime_instance_id="runtime-explicit",
    )
    matched = resolve_route_binding(
        tmp_path,
        target_hint=beta_target,
        runtime_instance_id="runtime-match",
    )

    assert explicit is not None and explicit.project_id == "alpha"
    assert explicit.binding_source == "explicit"
    assert matched is not None and matched.project_id == "beta"
    assert matched.binding_source == "target_match"
    with pytest.raises(ValueError, match="conflicts with target hint"):
        resolve_route_binding(
            tmp_path,
            project="alpha",
            target_hint=beta_target,
            runtime_instance_id="runtime-conflict",
        )


def test_resolve_route_binding_target_match_fails_closed(tmp_path: Path) -> None:
    shared_target = tmp_path / "shared-target"
    project_action(tmp_path, "create", name="alpha", target=shared_target, output_format="dict")
    project_action(tmp_path, "create", name="beta", target=shared_target, output_format="dict")

    with pytest.raises(ValueError, match="matches multiple managed projects.*alpha, beta"):
        resolve_route_binding(
            tmp_path,
            target_hint=shared_target,
            runtime_instance_id="runtime-ambiguous",
        )
    with pytest.raises(FileNotFoundError, match="No managed project target matches"):
        resolve_route_binding(
            tmp_path,
            target_hint=tmp_path / "missing-target",
            runtime_instance_id="runtime-missing",
        )


def test_legacy_selector_binding_requires_explicit_compatibility(tmp_path: Path) -> None:
    project_action(tmp_path, "create", name="alpha", output_format="dict")

    with pytest.raises(RuntimeError, match="legacy selector fallback is disabled"):
        resolve_route_binding(tmp_path, runtime_instance_id="runtime-bound")

    legacy = resolve_route_binding(
        tmp_path,
        runtime_instance_id="runtime-interactive",
        allow_legacy_selector=True,
    )

    assert legacy is not None
    assert legacy.project_id == "alpha"
    assert legacy.binding_source == "legacy_selector"


def test_bound_route_fingerprint_ignores_selector_but_detects_retarget(tmp_path: Path) -> None:
    alpha_target = tmp_path / "alpha-target"
    project_action(tmp_path, "create", name="alpha", target=alpha_target, output_format="dict")
    project_action(tmp_path, "create", name="beta", target=tmp_path / "beta-target", output_format="dict")
    binding = resolve_route_binding(
        tmp_path,
        project="alpha",
        runtime_instance_id="runtime-a",
    )
    assert binding is not None
    initial = project_routing_fingerprint(tmp_path, binding)

    selector = tmp_path / "workspace" / ".active_project"
    selector.write_text("beta\n", encoding="utf-8")
    assert project_routing_fingerprint(tmp_path, binding) == initial
    selector.write_text("../corrupt\n", encoding="utf-8")
    assert project_routing_fingerprint(tmp_path, binding) == initial
    selector.unlink()
    assert project_routing_fingerprint(tmp_path, binding) == initial

    descriptor = Path(binding.artifact_root) / "PROJECT.md"
    descriptor.write_text(
        descriptor.read_text(encoding="utf-8").replace(
            str(alpha_target.resolve()),
            str((tmp_path / "retargeted-alpha").resolve()),
        ),
        encoding="utf-8",
    )
    assert project_routing_fingerprint(tmp_path, binding) != initial


def test_route_path_identity_preserves_host_boundaries(tmp_path: Path) -> None:
    real = tmp_path / "CaseSensitive"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)

    assert route_path_identity(f"{real}/") == route_path_identity(alias)
    assert route_path_identity(tmp_path / "CaseSensitive") != route_path_identity(tmp_path / "casesensitive")
    assert route_path_identity("C:\\Users\\Henry\\Repo\\", platform="windows") == route_path_identity(
        "c:/users/henry/repo",
        platform="windows",
    )
    assert route_path_identity("/Workspace/Repos/henry/anchor") != route_path_identity(
        "/dbfs/Workspace/Repos/henry/anchor"
    )


def test_bound_runtime_roots_and_project_use_ignore_selector_authority(tmp_path: Path) -> None:
    alpha_target = tmp_path / "alpha-target"
    beta_target = tmp_path / "beta-target"
    project_action(tmp_path, "create", name="alpha", target=alpha_target, output_format="dict")
    project_action(tmp_path, "create", name="beta", target=beta_target, output_format="dict")
    binding = resolve_route_binding(
        tmp_path,
        project="alpha",
        runtime_instance_id="runtime-alpha",
    )
    assert binding is not None

    selected = project_action(
        tmp_path,
        "use",
        "beta",
        current_project="alpha",
        current_target=str(alpha_target),
        route_binding=binding,
        output_format="dict",
    )
    roots = resolve_runtime_roots(tmp_path, route_binding=binding)

    assert selected["active_project"] == "beta"
    assert selected["runtime_binding_unchanged"] is True
    assert selected["reinitialize_required"] is False
    assert selected["route_binding"]["project_id"] == "alpha"
    assert roots.active_project == "alpha"
    assert roots.target_root == str(alpha_target.resolve())
    assert roots.artifact_root == binding.artifact_root


def test_binding_diagnostics_are_safe_and_registry_mutation_is_precise(tmp_path: Path) -> None:
    alpha_target = tmp_path / "alpha-target"
    project_action(tmp_path, "create", name="alpha", target=alpha_target, output_format="dict")
    binding = resolve_route_binding(
        tmp_path,
        project="alpha",
        runtime_instance_id="runtime-alpha",
        host_thread_correlation="private-thread",
    )
    assert binding is not None

    diagnostics = route_binding_diagnostics(binding)
    assert diagnostics["binding_source"] == "explicit"
    assert diagnostics["legacy_selector_used"] is False
    assert diagnostics["legacy_selector_fallback_count"] == 0
    assert "host_thread_correlation" not in diagnostics
    assert "private-thread" not in str(diagnostics)
    assert route_binding_staleness_reason(binding) is None

    descriptor = Path(binding.artifact_root) / "PROJECT.md"
    descriptor.write_text(
        descriptor.read_text(encoding="utf-8").replace(
            str(alpha_target.resolve()),
            str((tmp_path / "retargeted-alpha").resolve()),
        ),
        encoding="utf-8",
    )
    assert route_binding_staleness_reason(binding) == (
        "bound managed project 'alpha' target root changed"
    )
    descriptor.unlink()
    assert route_binding_staleness_reason(binding) == (
        "bound managed project 'alpha' no longer exists"
    )
