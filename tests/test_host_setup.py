from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

import odibi_anchor.host_setup as module
from odibi_anchor.host_setup import HostSetupError, setup_host

ROOT = Path(__file__).resolve().parents[1]


def _resources(tmp_path: Path) -> Path:
    root = tmp_path / "resources"
    shutil.copytree(ROOT / ".assistant", root / ".assistant")
    shutil.copy2(ROOT / ".assistant_instructions.md", root / ".assistant_instructions.md")
    shutil.copy2(ROOT / "agent_bootstrap.py", root / "agent_bootstrap.py")
    return root


def test_fresh_install_and_idempotence(tmp_path, monkeypatch):
    resources = _resources(tmp_path)
    monkeypatch.setattr("odibi_anchor._runtime_paths.resolve_resource_root", lambda: resources)
    target = tmp_path / "target"
    target.mkdir()

    first = setup_host(target, adapter="amp")
    second = setup_host(target, adapter="amp")

    assert first["status"] == "installed"
    assert second["status"] == "unchanged"
    assert first["managed_files"] == second["managed_files"]
    assert (target / "AGENTS.md").is_file()
    json.dumps(first)


def test_existing_host_guidance_pointer_is_preserved_unmanaged(tmp_path, monkeypatch):
    resources = _resources(tmp_path)
    monkeypatch.setattr("odibi_anchor._runtime_paths.resolve_resource_root", lambda: resources)
    target = tmp_path / "target"
    target.mkdir()
    existing = "# Team rules\nRead .assistant_instructions.md before work.\n"
    (target / "AGENTS.md").write_text(existing)

    result = setup_host(target, adapter="amp")
    repeated = setup_host(target, adapter="amp")

    assert (target / "AGENTS.md").read_text() == existing
    assert result["compatible_unmanaged_files"] == ["AGENTS.md"]
    assert repeated["status"] == "unchanged"
    assert repeated["compatible_unmanaged_files"] == ["AGENTS.md"]
    assert "AGENTS.md" not in json.loads((target / module._MANIFEST).read_text())["files"]


def test_upgrades_only_unchanged_managed_content(tmp_path, monkeypatch):
    resources = _resources(tmp_path)
    monkeypatch.setattr("odibi_anchor._runtime_paths.resolve_resource_root", lambda: resources)
    target = tmp_path / "target"
    target.mkdir()
    setup_host(target, adapter="claude")
    (resources / ".assistant" / "README.md").write_text("new packaged content\n")

    result = setup_host(target, adapter="claude")

    assert result["status"] == "upgraded"
    assert (target / ".assistant" / "README.md").read_text() == "new packaged content\n"


def test_upgrade_removes_only_unchanged_files_no_longer_packaged(tmp_path, monkeypatch):
    resources = _resources(tmp_path)
    monkeypatch.setattr("odibi_anchor._runtime_paths.resolve_resource_root", lambda: resources)
    target = tmp_path / "target"
    target.mkdir()
    setup_host(target, adapter="amp")
    removed = target / ".assistant" / "README.md"
    assert removed.is_file()
    (resources / ".assistant" / "README.md").unlink()

    result = setup_host(target, adapter="amp")

    assert result["status"] == "upgraded"
    assert not removed.exists()


@pytest.mark.parametrize("managed", [False, True])
def test_collision_refusal_has_no_partial_changes(tmp_path, monkeypatch, managed):
    resources = _resources(tmp_path)
    monkeypatch.setattr("odibi_anchor._runtime_paths.resolve_resource_root", lambda: resources)
    target = tmp_path / "target"
    target.mkdir()
    if managed:
        setup_host(target, adapter="amp")
        collision = target / ".assistant_instructions.md"
    else:
        collision = target / "AGENTS.md"
    collision.write_text("user edit\n")
    before = {p.relative_to(target): p.read_bytes() for p in target.rglob("*") if p.is_file()}

    with pytest.raises(HostSetupError, match=r"modified managed|unmanaged destination collision"):
        setup_host(target, adapter="amp")

    after = {p.relative_to(target): p.read_bytes() for p in target.rglob("*") if p.is_file()}
    assert after == before


@pytest.mark.parametrize(
    ("adapter", "host_file"),
    [("amp", "AGENTS.md"), ("claude", "CLAUDE.md"),
     ("databricks", "agent_bootstrap.py"), ("chatgpt", "AGENTS.md")],
)
def test_adapter_specific_installation(tmp_path, monkeypatch, adapter, host_file):
    resources = _resources(tmp_path)
    monkeypatch.setattr("odibi_anchor._runtime_paths.resolve_resource_root", lambda: resources)
    target = tmp_path / adapter
    target.mkdir()

    result = setup_host(target, adapter=adapter)

    assert result["adapter"] == adapter
    assert (target / host_file).is_file()


def test_databricks_installs_complete_authored_guidance_without_snapshot_cache(
    tmp_path, monkeypatch
):
    resources = _resources(tmp_path)
    monkeypatch.setattr("odibi_anchor._runtime_paths.resolve_resource_root", lambda: resources)
    target = tmp_path / "databricks"
    target.mkdir()

    result = setup_host(target, adapter="databricks")

    assert result["resource_profile"] == "databricks_workspace_compact"
    assert result["omitted_packaged_prefixes"] == [
        ".assistant/references/snapshots/"
    ]
    assert (target / ".assistant" / "skills" / "debugging" / "SKILL.md").is_file()
    assert (
        target / ".assistant" / "references" / "odibi-anchor" / "workflow.md"
    ).is_file()
    assert not (target / ".assistant" / "references" / "snapshots").exists()
    assert all(
        not item["path"].startswith(".assistant/references/snapshots/")
        for item in result["managed_files"]
    )


def test_other_adapters_retain_snapshot_cache(tmp_path, monkeypatch):
    resources = _resources(tmp_path)
    monkeypatch.setattr("odibi_anchor._runtime_paths.resolve_resource_root", lambda: resources)
    target = tmp_path / "amp"
    target.mkdir()

    result = setup_host(target, adapter="amp")

    assert result["resource_profile"] == "complete"
    assert result["omitted_packaged_prefixes"] == []
    assert (target / ".assistant" / "references" / "snapshots" / "manifest.json").is_file()


@pytest.mark.parametrize("content", ["not json", '{"version": 99, "adapter": "amp", "files": {}}'])
def test_malformed_manifest_is_refused(tmp_path, monkeypatch, content):
    resources = _resources(tmp_path)
    monkeypatch.setattr("odibi_anchor._runtime_paths.resolve_resource_root", lambda: resources)
    target = tmp_path / "target"
    target.mkdir()
    (target / module._MANIFEST).write_text(content)

    with pytest.raises(HostSetupError, match="manifest is malformed"):
        setup_host(target, adapter="amp")
    assert set(target.iterdir()) == {target / module._MANIFEST}


def test_tampered_manifest_hash_is_refused(tmp_path, monkeypatch):
    resources = _resources(tmp_path)
    monkeypatch.setattr("odibi_anchor._runtime_paths.resolve_resource_root", lambda: resources)
    target = tmp_path / "target"
    target.mkdir()
    setup_host(target, adapter="amp")
    manifest_path = target / module._MANIFEST
    manifest = json.loads(manifest_path.read_text())
    manifest["files"]["AGENTS.md"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")

    with pytest.raises(HostSetupError, match=r"modified managed file: AGENTS\.md"):
        setup_host(target, adapter="amp")


def test_symlink_target_and_destination_are_refused(tmp_path, monkeypatch):
    resources = _resources(tmp_path)
    monkeypatch.setattr("odibi_anchor._runtime_paths.resolve_resource_root", lambda: resources)
    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)
    with pytest.raises(ValueError, match="not a symlink"):
        setup_host(linked, adapter="amp")

    target = tmp_path / "target"
    outside = tmp_path / "outside"
    target.mkdir()
    outside.mkdir()
    (target / ".assistant").symlink_to(outside, target_is_directory=True)
    with pytest.raises(HostSetupError, match="symlink"):
        setup_host(target, adapter="amp")
    assert not any(outside.iterdir())


def test_publication_and_rollback_failure_preserves_recovery_backups(
    tmp_path, monkeypatch
):
    resources = _resources(tmp_path)
    monkeypatch.setattr("odibi_anchor._runtime_paths.resolve_resource_root", lambda: resources)
    target = tmp_path / "target"
    target.mkdir()
    setup_host(target, adapter="amp")
    (resources / ".assistant" / "README.md").write_text("new packaged content\n")
    real_replace = module.os.replace

    def failing_replace(source, destination):
        source_path = Path(source)
        destination_path = Path(destination)
        if "new" in source_path.parts and destination_path.name == "README.md":
            raise OSError("publication failed")
        if "backup" in source_path.parts:
            raise OSError("rollback failed")
        return real_replace(source, destination)

    monkeypatch.setattr(module.os, "replace", failing_replace)
    with pytest.raises(HostSetupError, match="backups preserved at"):
        setup_host(target, adapter="amp")

    staging = list(target.glob(".anchor-host-stage-*"))
    assert len(staging) == 1
    assert any((staging[0] / "backup").rglob("*"))


def test_publication_interrupt_restores_original_or_preserves_backup(tmp_path, monkeypatch):
    resources = _resources(tmp_path)
    monkeypatch.setattr("odibi_anchor._runtime_paths.resolve_resource_root", lambda: resources)
    target = tmp_path / "target"
    target.mkdir()
    setup_host(target, adapter="amp")
    managed = target / ".assistant" / "README.md"
    original = managed.read_bytes()
    (resources / ".assistant" / "README.md").write_text("new packaged content\n")
    real_replace = module.os.replace

    def interrupted_replace(source, destination):
        source_path = Path(source)
        destination_path = Path(destination)
        if "new" in source_path.parts and destination_path == managed:
            raise KeyboardInterrupt("publication interrupted")
        return real_replace(source, destination)

    monkeypatch.setattr(module.os, "replace", interrupted_replace)
    with pytest.raises(KeyboardInterrupt, match="publication interrupted"):
        setup_host(target, adapter="amp")

    assert managed.read_bytes() == original
    assert not list(target.glob(".anchor-host-stage-*"))


@pytest.mark.parametrize("retire", [False, True])
def test_interrupt_immediately_after_backup_move_cannot_delete_original(
    tmp_path, monkeypatch, retire
):
    resources = _resources(tmp_path)
    monkeypatch.setattr("odibi_anchor._runtime_paths.resolve_resource_root", lambda: resources)
    target = tmp_path / "target"
    target.mkdir()
    setup_host(target, adapter="amp")
    managed = target / ".assistant" / "README.md"
    original = managed.read_bytes()
    if retire:
        (resources / ".assistant" / "README.md").unlink()
    else:
        (resources / ".assistant" / "README.md").write_text("new packaged content\n")
    real_replace = module.os.replace

    def interrupt_after_move(source, destination):
        source_path = Path(source)
        destination_path = Path(destination)
        if source_path == managed and "backup" in destination_path.parts:
            real_replace(source, destination)
            raise KeyboardInterrupt("interrupted after backup move")
        return real_replace(source, destination)

    monkeypatch.setattr(module.os, "replace", interrupt_after_move)
    with pytest.raises(KeyboardInterrupt, match="interrupted after backup move"):
        setup_host(target, adapter="amp")

    assert managed.read_bytes() == original
    assert not list(target.glob(".anchor-host-stage-*"))
