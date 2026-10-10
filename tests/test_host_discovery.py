"""Deterministic portfolio discovery, host-guidance reconcile and the host-root launcher (#30)."""
from __future__ import annotations

import json
import runpy
import shutil
from pathlib import Path
from typing import Any, cast

import pytest

import odibi_anchor.host_setup as module
from odibi_anchor import cli
from odibi_anchor.host_setup import HostSetupError, setup_host
from odibi_anchor.portfolio import write_portfolio
from tests.test_host_setup import _resources, _workspace_setup

REPOSITORY = Path(__file__).resolve().parents[1]
BINDING = ".odibi-anchor-host-binding.json"
BACKUPS = ".odibi-anchor-host-backups"
MANIFEST = ".odibi-anchor-host-guidance.json"
WORKSPACE_ROOT = "/Workspace/Users/test@example.invalid/anchor-host"


def _portfolio(config: Path, instruction_root: str, *, host_id: str = "work") -> Path:
    config.parent.mkdir(parents=True, exist_ok=True)
    write_portfolio(config, {
        "schema_version": 1,
        "authority": {"id": "owner", "trust_domain": "work"},
        "hosts": {host_id: {
            "adapter": "databricks", "local_state_root": "/tmp/anchor-state",
            "instruction_root": instruction_root, "durable_root": "/Volumes/c/s/anchor",
        }},
        "projects": {}, "personas": {},
    })
    return config


def _binding_bytes(config: Path | str, instruction_root: Path | str, host_id: str = "work") -> bytes:
    """The sidecar format, derived independently of host_setup."""
    value = {
        "config_path": str(config), "host_id": host_id,
        "instruction_root": str(instruction_root), "version": 1,
    }
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def _tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*")) if path.is_file()
    }


# ── managed launcher discovery ───────────────────────────────────────────────


@pytest.fixture
def launcher_host(tmp_path, monkeypatch):
    """A host root with the managed launcher and a recorded bootstrap call."""
    import odibi_anchor

    host = (tmp_path / "host").resolve()
    launcher = host / ".assistant" / "agent_bootstrap.py"
    launcher.parent.mkdir(parents=True)
    shutil.copy2(REPOSITORY / ".assistant" / "agent_bootstrap.py", launcher)
    observed: dict[str, Any] = {}

    def fake_bootstrap(**kwargs):
        observed.update(kwargs)
        return {
            "anchor": lambda *_a, **_k: {}, "root": str(tmp_path), "manifest": None,
            "orientation": {"kind": "orientation"},
            "startup_packet": {"kind": "managed_startup_packet", "status": "ready"},
            "preparation": {"environment": {"ANCHOR_HOME": str(tmp_path / "state")}},
        }

    monkeypatch.setattr(odibi_anchor, "bootstrap_managed_project", fake_bootstrap)
    monkeypatch.setattr(
        odibi_anchor, "launch", lambda **_k: pytest.fail("installed fallback must not run")
    )
    for name in ("DATABRICKS_RUNTIME_VERSION", "ANCHOR_SOURCE_CHECKOUT", "ANCHOR_PROJECT_ID",
                 "ANCHOR_PORTFOLIO_CONFIG", "ANCHOR_HOME"):
        monkeypatch.delenv(name, raising=False)

    def run(**init_globals: Any) -> dict[str, Any]:
        runpy.run_path(str(launcher), init_globals={"ANCHOR_PROJECT_ID": "alpha", **init_globals})
        return observed

    return host, run


def test_launcher_selects_sidecar_portfolio_recorded_by_setup_host(launcher_host, tmp_path):
    host, run = launcher_host
    config = _portfolio(tmp_path / "home" / ".odibi-anchor" / "anchor.toml", str(host))
    recorded = setup_host(host, adapter="databricks", portfolio_config=config)

    observed = run()

    assert recorded["host_binding"]["status"] == "created"
    assert observed["config_path"] == config
    assert observed["host_id"] == "work"
    assert observed["instruction_root"] == host


@pytest.mark.parametrize("source", ["init_global", "environment"])
def test_explicit_portfolio_outranks_sidecar(launcher_host, tmp_path, monkeypatch, source):
    host, run = launcher_host
    explicit = _portfolio(tmp_path / "explicit" / "anchor.toml", str(host))
    # Lower precedence, so it is never consulted even though it is invalid.
    (host / BINDING).write_text('{"version": 1}')
    init_globals = {}
    if source == "init_global":
        init_globals["ANCHOR_PORTFOLIO_CONFIG"] = str(explicit)
    else:
        monkeypatch.setenv("ANCHOR_PORTFOLIO_CONFIG", str(explicit))

    observed = run(**init_globals)

    assert observed["config_path"] == explicit
    assert "host_id" not in observed


def test_sidecar_and_different_default_are_ambiguous(launcher_host, tmp_path):
    host, run = launcher_host
    bound = _portfolio(tmp_path / "home" / ".odibi-anchor" / "anchor.toml", str(host))
    default = _portfolio(host / ".odibi-anchor" / "anchor.toml", str(host))
    (host / BINDING).write_bytes(_binding_bytes(bound, host))

    with pytest.raises(RuntimeError) as raised:
        run()

    error = cast(Any, raised.value)
    assert error.error_code == "managed_portfolio_ambiguous"
    assert [(entry["precedence"], entry["path"], entry["status"])
            for entry in error.context["searched_paths"]] == [
        (1, None, "unset"), (2, None, "unset"),
        (3, str(bound), "selected"), (4, str(default), "conflicts_with_selected"),
    ]
    assert {operation["operation"] for operation in error.next_operations} >= {
        "rerun_launcher_with_portfolio", "setup_host.record_portfolio",
    }
    assert all(operation["requires_owner"] for operation in error.next_operations)


def test_sidecar_naming_the_default_is_not_ambiguous(launcher_host):
    host, run = launcher_host
    default = _portfolio(host / ".odibi-anchor" / "anchor.toml", str(host))
    (host / BINDING).write_bytes(_binding_bytes(default, host))

    observed = run()

    assert observed["config_path"] == default
    assert observed["host_id"] == "work"


@pytest.mark.parametrize("damage", [
    "non_canonical", "relative_config", "other_instruction_root", "host_not_bound_here",
])
def test_invalid_sidecar_fails_closed(launcher_host, tmp_path, damage):
    host, run = launcher_host
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    config = _portfolio(
        tmp_path / "home" / "anchor.toml",
        str(elsewhere) if damage == "host_not_bound_here" else str(host),
    )
    content = {
        "non_canonical": json.dumps({
            "config_path": str(config), "host_id": "work", "instruction_root": str(host),
            "version": 1,
        }).encode(),
        "relative_config": _binding_bytes("home/anchor.toml", host),
        "other_instruction_root": _binding_bytes(config, elsewhere),
        "host_not_bound_here": _binding_bytes(config, host),
    }[damage]
    (host / BINDING).write_bytes(content)

    with pytest.raises(RuntimeError) as raised:
        run()

    error = cast(Any, raised.value)
    assert error.error_code == "managed_host_binding_invalid"
    assert error.context["binding_path"] == str(host / BINDING)


def test_sidecar_with_missing_portfolio_refuses_installed_fallback(
    launcher_host, tmp_path, monkeypatch,
):
    host, run = launcher_host
    missing = tmp_path / "gone" / "anchor.toml"
    (host / BINDING).write_bytes(_binding_bytes(missing, host))
    monkeypatch.setenv("ANCHOR_HOME", str(tmp_path / "state"))

    with pytest.raises(RuntimeError) as raised:
        run()

    error = cast(Any, raised.value)
    assert error.error_code == "managed_portfolio_not_found"
    assert error.context["selected_path"] == str(missing)
    assert error.context["binding"]["host_id"] == "work"


def test_nested_install_is_detected_and_named(tmp_path, monkeypatch):
    home = tmp_path / "home"
    nested_launcher = home / ".assistant" / ".assistant" / "agent_bootstrap.py"
    nested_launcher.parent.mkdir(parents=True)
    shutil.copy2(REPOSITORY / ".assistant" / "agent_bootstrap.py", nested_launcher)
    probable_launcher = home / ".assistant" / "agent_bootstrap.py"
    shutil.copy2(REPOSITORY / ".assistant" / "agent_bootstrap.py", probable_launcher)
    _portfolio(home / ".odibi-anchor" / "anchor.toml", str(home))
    for name in ("DATABRICKS_RUNTIME_VERSION", "ANCHOR_SOURCE_CHECKOUT", "ANCHOR_PROJECT_ID",
                 "ANCHOR_PORTFOLIO_CONFIG", "ANCHOR_HOME"):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(RuntimeError) as raised:
        runpy.run_path(str(nested_launcher), init_globals={"ANCHOR_PROJECT_ID": "alpha"})

    error = cast(Any, raised.value)
    assert error.error_code == "managed_portfolio_not_found"
    nested = error.context["nested_install"]
    assert nested["probable_launcher"] == str(probable_launcher.resolve())
    assert nested["probable_portfolio_exists"] is True
    assert error.context["selected_path"] == str(
        (home / ".assistant" / ".odibi-anchor" / "anchor.toml").resolve()
    )
    assert "Nested install detected" in str(error)
    run_parent = [op for op in error.next_operations if op["operation"] == "run_probable_launcher"]
    assert str(probable_launcher.resolve()) in run_parent[0]["copy_ready"]


def test_host_without_sidecar_keeps_v0324_discovery(launcher_host):
    host, run = launcher_host
    default = _portfolio(host / ".odibi-anchor" / "anchor.toml", str(host))

    observed = run()

    assert observed == {
        "config_path": default, "project_id": "alpha", "instruction_root": host,
        "create_if_missing": False, "project_root": None,
    }


# ── host binding sidecar ─────────────────────────────────────────────────────


def test_binding_is_canonical_outside_the_manifest_and_idempotent(tmp_path, monkeypatch):
    resources = _resources(tmp_path)
    monkeypatch.setattr("odibi_anchor._runtime_paths.resolve_resource_root", lambda: resources)
    host = (tmp_path / "host").resolve()
    host.mkdir()
    config = _portfolio(tmp_path / "anchor.toml", str(host))

    first = setup_host(host, adapter="databricks", portfolio_config=config)
    repeated = setup_host(host, adapter="databricks", portfolio_config=config)
    plain = setup_host(host, adapter="databricks")

    assert (host / BINDING).read_bytes() == _binding_bytes(config, host)
    assert BINDING not in json.loads((host / MANIFEST).read_text())["files"]
    assert (first["host_binding"]["status"], repeated["host_binding"]["status"]) == (
        "created", "unchanged",
    )
    assert plain["status"] == "unchanged"
    binding = module.read_host_binding(host)
    assert binding is not None and binding["config_path"] == str(config)


def test_binding_refuses_a_portfolio_host_for_another_root_before_writing(tmp_path, monkeypatch):
    resources = _resources(tmp_path)
    monkeypatch.setattr("odibi_anchor._runtime_paths.resolve_resource_root", lambda: resources)
    host = (tmp_path / "host").resolve()
    host.mkdir()
    config = _portfolio(tmp_path / "anchor.toml", str(tmp_path / "other"))

    with pytest.raises(HostSetupError) as raised:
        setup_host(host, adapter="databricks", portfolio_config=config)

    assert cast(Any, raised.value).error_code == "host_binding_mismatch"
    assert list(host.iterdir()) == []


# ── drift classification and reconcile ───────────────────────────────────────

LEGACY_LAUNCHER = b"# launcher bytes shipped by an earlier release\n"


@pytest.fixture
def drifted_host(tmp_path, monkeypatch):
    """An installed host with one released-version file, one edit and one deletion."""
    resources = _resources(tmp_path)
    monkeypatch.setattr("odibi_anchor._runtime_paths.resolve_resource_root", lambda: resources)
    monkeypatch.setattr(module, "_released_table", lambda: {
        ".assistant/agent_bootstrap.py": {module._sha256(LEGACY_LAUNCHER): ["0.3.20", "0.3.22"]},
    })
    host = (tmp_path / "host").resolve()
    host.mkdir()
    setup_host(host, adapter="databricks")
    (host / ".assistant" / "agent_bootstrap.py").write_bytes(LEGACY_LAUNCHER)
    (host / ".assistant" / "README.md").write_text("team edit\n")
    (host / ".assistant" / "references" / "odibi-anchor" / "workflow.md").unlink()
    return host


def _by_path(entries: list[dict[str, Any]]) -> dict[str, tuple[str, str]]:
    return {
        entry["path"]: (entry["classification"], entry["action"])
        for entry in entries if entry["classification"] != "current"
    }


EXPECTED_DRIFT = {
    ".assistant/README.md": ("unmanaged_edit", "replace"),
    ".assistant/agent_bootstrap.py": ("released_version", "replace"),
    ".assistant/references/odibi-anchor/workflow.md": ("missing", "install"),
}


def test_plain_setup_reports_classified_drift_without_writing(drifted_host):
    before = _tree(drifted_host)

    with pytest.raises(HostSetupError) as raised:
        setup_host(drifted_host, adapter="databricks")

    error = cast(Any, raised.value)
    assert error.error_code == "host_guidance_drift"
    assert str(error).startswith("modified managed file: .assistant/README.md (expected ")
    assert _by_path(error.context["files"]) == EXPECTED_DRIFT
    released = next(e for e in error.context["files"] if e["path"].endswith("agent_bootstrap.py"))
    assert released["released_versions"] == ["0.3.20", "0.3.22"]
    assert error.next_operation["copy_ready"] == (
        f"anchor setup-host databricks --target {drifted_host} --reconcile"
    )
    assert error.retry_safety == "read_only"
    assert _tree(drifted_host) == before


def test_reconcile_dry_run_returns_full_plan_and_writes_nothing(drifted_host):
    before = _tree(drifted_host)

    plan = setup_host(drifted_host, adapter="databricks", reconcile=True)

    assert (plan["status"], plan["dry_run"]) == ("planned", True)
    assert _by_path(plan["files"]) == EXPECTED_DRIFT
    assert plan["classification_counts"]["unmanaged_edit"] == 1
    assert plan["approval_required"] == [".assistant/README.md"]
    assert sorted(plan["backup"]["files"]) == [
        ".assistant/README.md", ".assistant/agent_bootstrap.py", MANIFEST,
    ]
    assert plan["next_operation"]["copy_ready"].endswith("--reconcile --apply --approve-replace-edited")
    assert plan["next_operation"]["requires_owner"] is True
    assert _tree(drifted_host) == before


def test_reconcile_refuses_unmanaged_edits_without_approval(drifted_host):
    before = _tree(drifted_host)

    with pytest.raises(HostSetupError) as raised:
        setup_host(drifted_host, adapter="databricks", reconcile=True, dry_run=False)

    error = cast(Any, raised.value)
    assert error.error_code == "host_guidance_edit_approval_required"
    assert [entry["path"] for entry in error.context["unmanaged_edits"]] == [".assistant/README.md"]
    assert error.next_operation["arguments"]["approve_replace_edited"] is True
    assert _tree(drifted_host) == before
    assert not (drifted_host / BACKUPS).exists()


def test_approved_reconcile_backs_up_reinstalls_and_verifies(drifted_host, tmp_path):
    old_manifest = (drifted_host / MANIFEST).read_bytes()
    desired = module._desired_files("databricks")

    result = setup_host(
        drifted_host, adapter="databricks", reconcile=True, dry_run=False,
        approve_replace_edited=True,
    )

    backup = Path(result["backup"]["root"])
    assert backup.parent == drifted_host / BACKUPS
    assert (backup / ".assistant" / "README.md").read_text() == "team edit\n"
    assert (backup / ".assistant" / "agent_bootstrap.py").read_bytes() == LEGACY_LAUNCHER
    assert (backup / MANIFEST).read_bytes() == old_manifest
    receipt = json.loads((backup / "BACKUP.json").read_text())
    assert {entry["path"]: entry["classification"] for entry in receipt["files"]} == {
        ".assistant/README.md": "unmanaged_edit",
        ".assistant/agent_bootstrap.py": "released_version",
    }
    for relative, content in desired.items():
        assert (drifted_host / relative).read_bytes() == content
    manifest = json.loads((drifted_host / MANIFEST).read_text())
    assert manifest["files"] == {relative: module._sha256(content) for relative, content in desired.items()}
    assert result["status"] == "reconciled"
    assert setup_host(drifted_host, adapter="databricks")["status"] == "unchanged"
    assert setup_host(drifted_host, adapter="databricks", reconcile=True)["status"] == "unchanged"


def test_reconcile_refuses_an_edit_made_after_the_plan_and_keeps_it(drifted_host, monkeypatch):
    readme = drifted_host / ".assistant" / "README.md"
    real_backup = module._backup_local

    def backup_then_user_edit(*args, **kwargs):
        root = real_backup(*args, **kwargs)
        readme.write_text("team edit\nUSER EDIT 2\n")  # lands between backup and publish
        return root

    monkeypatch.setattr(module, "_backup_local", backup_then_user_edit)
    with pytest.raises(HostSetupError, match="changed after the reconcile plan read it"):
        setup_host(
            drifted_host, adapter="databricks", reconcile=True, dry_run=False,
            approve_replace_edited=True,
        )

    assert readme.read_text() == "team edit\nUSER EDIT 2\n"
    assert (drifted_host / ".assistant" / "agent_bootstrap.py").read_bytes() == LEGACY_LAUNCHER
    assert not list(drifted_host.glob(".anchor-host-stage-*"))


def test_workspace_reconcile_refuses_an_edit_made_after_the_plan(tmp_path, monkeypatch):
    _resources_root, workspace = _workspace_setup(tmp_path, monkeypatch)
    api_root = WORKSPACE_ROOT.removeprefix("/Workspace")
    setup_host(WORKSPACE_ROOT, adapter="databricks")
    edited = f"{api_root}/.assistant/README.md"
    workspace.files[edited] = b"team edit\n"
    real_backup = module._backup_workspace

    def backup_then_user_edit(*args, **kwargs):
        root = real_backup(*args, **kwargs)
        workspace.files[edited] = b"team edit\nUSER EDIT 2\n"
        return root

    monkeypatch.setattr(module, "_backup_workspace", backup_then_user_edit)
    with pytest.raises(HostSetupError, match="changed after the reconcile plan read it"):
        setup_host(
            WORKSPACE_ROOT, adapter="databricks", reconcile=True, dry_run=False,
            approve_replace_edited=True,
        )

    assert workspace.files[edited] == b"team edit\nUSER EDIT 2\n"


def test_released_version_drift_applies_without_edit_approval(tmp_path, monkeypatch):
    resources = _resources(tmp_path)
    monkeypatch.setattr("odibi_anchor._runtime_paths.resolve_resource_root", lambda: resources)
    monkeypatch.setattr(module, "_released_table", lambda: {
        ".assistant/agent_bootstrap.py": {module._sha256(LEGACY_LAUNCHER): ["0.3.20", "0.3.22"]},
    })
    host = (tmp_path / "host").resolve()
    host.mkdir()
    setup_host(host, adapter="databricks")
    (host / ".assistant" / "agent_bootstrap.py").write_bytes(LEGACY_LAUNCHER)

    result = setup_host(host, adapter="databricks", reconcile=True, dry_run=False)

    assert result["status"] == "reconciled"
    assert result["approval_required"] == []
    assert (Path(result["backup"]["root"]) / ".assistant" / "agent_bootstrap.py").read_bytes() == (
        LEGACY_LAUNCHER
    )


def test_obsolete_edited_file_is_backed_up_before_removal(tmp_path, monkeypatch):
    resources = _resources(tmp_path)
    monkeypatch.setattr("odibi_anchor._runtime_paths.resolve_resource_root", lambda: resources)
    host = (tmp_path / "host").resolve()
    host.mkdir()
    setup_host(host, adapter="databricks")
    (resources / ".assistant" / "README.md").unlink()  # no longer packaged
    (host / ".assistant" / "README.md").write_text("team edit\n")

    plan = setup_host(host, adapter="databricks", reconcile=True)
    result = setup_host(
        host, adapter="databricks", reconcile=True, dry_run=False, approve_replace_edited=True,
    )

    assert _by_path(plan["files"]) == {".assistant/README.md": ("unmanaged_edit", "delete")}
    assert not (host / ".assistant" / "README.md").exists()
    assert (Path(result["backup"]["root"]) / ".assistant" / "README.md").read_text() == "team edit\n"
    assert ".assistant/README.md" not in json.loads((host / MANIFEST).read_text())["files"]


def test_workspace_reconcile_and_binding_use_the_workspace_api(tmp_path, monkeypatch):
    _resources_root, workspace = _workspace_setup(tmp_path, monkeypatch)
    api_root = WORKSPACE_ROOT.removeprefix("/Workspace")
    setup_host(WORKSPACE_ROOT, adapter="databricks")
    edited = f"{api_root}/.assistant/README.md"
    original = workspace.files[edited]
    workspace.files[edited] = b"team edit\n"
    config = _portfolio(tmp_path / "anchor.toml", WORKSPACE_ROOT)

    with pytest.raises(HostSetupError) as raised:
        setup_host(WORKSPACE_ROOT, adapter="databricks")
    calls_before = len(workspace.calls)
    plan = setup_host(WORKSPACE_ROOT, adapter="databricks", reconcile=True)
    dry_run_calls = workspace.calls[calls_before:]
    result = setup_host(
        WORKSPACE_ROOT, adapter="databricks", reconcile=True, dry_run=False,
        approve_replace_edited=True, portfolio_config=config,
    )

    assert cast(Any, raised.value).error_code == "host_guidance_drift"
    assert _by_path(plan["files"]) == {".assistant/README.md": ("unmanaged_edit", "replace")}
    assert not any(operation in {"upload", "delete", "mkdirs"} for operation, _ in dry_run_calls)
    backup = result["backup"]["root"].removeprefix("/Workspace")
    assert workspace.files[f"{backup}/.assistant/README.md"] == b"team edit\n"
    assert json.loads(workspace.files[f"{backup}/BACKUP.json"])["files"][0]["classification"] == (
        "unmanaged_edit"
    )
    assert workspace.files[edited] == original
    assert workspace.files[f"{api_root}/{BINDING}"] == _binding_bytes(config, WORKSPACE_ROOT)
    assert result["host_binding"]["status"] == "created"
    assert setup_host(WORKSPACE_ROOT, adapter="databricks")["status"] == "unchanged"


def test_shipped_released_table_recognizes_v0324_launchers():
    module._released_table.cache_clear()
    table = module._released_table()

    def covers_v0324(span):
        # The upper bound grows when later tags ship the same bytes (regenerated per release).
        first, last = (tuple(int(part) for part in version.split(".")) for version in span)
        return first <= (0, 3, 24) <= last

    assert table[".assistant/agent_bootstrap.py"][
        "ad4c2bc8838f823bea9c27b9ebd2649ae2d02072a500e30e332f76f1197b8bde"
    ][0] == "0.3.24"
    assert covers_v0324(table[".assistant/agent_bootstrap.py"][
        "ad4c2bc8838f823bea9c27b9ebd2649ae2d02072a500e30e332f76f1197b8bde"
    ])
    assert covers_v0324(table["agent_bootstrap.py"][
        "02c55115d8ec9dc52bd102d1ef6b95a82bb945db5db5c0debe45284423a54993"
    ])


# ── host-root source launcher ────────────────────────────────────────────────


@pytest.mark.parametrize("managed_present", [True, False])
def test_host_root_source_launcher_points_at_managed_launcher(tmp_path, managed_present):
    host = tmp_path / "host"
    host.mkdir()
    shutil.copy2(REPOSITORY / "agent_bootstrap.py", host / "agent_bootstrap.py")
    managed = host / ".assistant" / "agent_bootstrap.py"
    if managed_present:
        managed.parent.mkdir()
        shutil.copy2(REPOSITORY / ".assistant" / "agent_bootstrap.py", managed)

    with pytest.raises(RuntimeError) as raised:
        runpy.run_path(str(host / "agent_bootstrap.py"))

    error = cast(Any, raised.value)
    assert error.error_code == "source_launcher_outside_checkout"
    assert error.context["managed_launcher"] == str(managed.resolve())
    assert error.context["managed_launcher_exists"] is managed_present
    assert (getattr(error, "copy_ready", None) is not None) is managed_present
    if managed_present:
        assert str(managed.resolve()) in error.copy_ready


# ── CLI ──────────────────────────────────────────────────────────────────────


def test_cli_reconcile_flags_and_fresh_compute_inputs(tmp_path, monkeypatch, capsys):
    resources = _resources(tmp_path)
    monkeypatch.setattr("odibi_anchor._runtime_paths.resolve_resource_root", lambda: resources)
    monkeypatch.setattr(cli, "_boot", lambda _root: pytest.fail("dispatcher booted"))
    host = (tmp_path / "host").resolve()
    host.mkdir()

    assert cli.main(["setup-host", "databricks", "--target", str(host), "--apply"]) == cli.EXIT_INPUT
    assert cli.main(["doctor", "--fresh-compute", "--host", "work"]) == cli.EXIT_INPUT
    assert cli.main(["doctor", "--config", str(tmp_path / "a.toml")]) == cli.EXIT_INPUT
    capsys.readouterr()
    assert cli.main(["setup-host", "databricks", "--target", str(host), "--reconcile"]) == 0
    plan = json.loads(capsys.readouterr().out)["result"]

    assert (plan["status"], plan["dry_run"], plan["classification_counts"]["missing"]) == (
        "planned", True, len(plan["files"]),
    )
    assert list(host.iterdir()) == []
