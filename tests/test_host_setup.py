from __future__ import annotations

import io
import json
import shutil
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import odibi_anchor.host_setup as module
from odibi_anchor._bootstrap_phases import BootstrapPhaseTimeout, phase, recording
from odibi_anchor.host_setup import HostSetupError, setup_host

ROOT = Path(__file__).resolve().parents[1]


def _resources(tmp_path: Path) -> Path:
    root = tmp_path / "resources"
    shutil.copytree(ROOT / ".assistant", root / ".assistant")
    shutil.copy2(ROOT / ".assistant_instructions.md", root / ".assistant_instructions.md")
    shutil.copy2(ROOT / "agent_bootstrap.py", root / "agent_bootstrap.py")
    return root


def _fake_databricks_sdk(workspace, configs: list | None = None):
    """Fake the SDK modules host setup imports; record each explicit client config."""

    def workspace_client(*, config):
        if configs is not None:
            configs.append(config)
        return SimpleNamespace(workspace=workspace)

    modules = {
        "databricks.sdk": SimpleNamespace(WorkspaceClient=workspace_client),
        "databricks.sdk.config": SimpleNamespace(Config=lambda **kwargs: kwargs),
        "databricks.sdk.service.workspace": SimpleNamespace(
            ImportFormat=SimpleNamespace(AUTO="AUTO")
        ),
    }

    def import_module(name: str):
        if name not in modules:
            pytest.fail(f"unexpected import: {name}")
        return modules[name]

    return import_module


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
    assert first["verified_file_count"] == len(first["managed_files"])
    assert first["verified_skill_count"] == 18
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


def test_legacy_anchor_install_without_manifest_is_safely_adopted(tmp_path, monkeypatch):
    resources = _resources(tmp_path)
    monkeypatch.setattr("odibi_anchor._runtime_paths.resolve_resource_root", lambda: resources)
    target = tmp_path / "target"
    target.mkdir()
    legacy_path = ".assistant/agent_bootstrap.py"
    legacy_content = b"released legacy bootstrap\n"
    legacy_digest = module._sha256(legacy_content)
    monkeypatch.setitem(module._LEGACY_MANAGED_HASHES, legacy_path, {legacy_digest})
    for relative, content in module._desired_files("databricks").items():
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(legacy_content if relative == legacy_path else content)

    result = setup_host(target, adapter="databricks")
    repeated = setup_host(target, adapter="databricks")

    assert result["status"] == "upgraded"
    assert result["reconciled_legacy_managed_files"] == [legacy_path]
    assert (target / legacy_path).read_bytes() == (resources / legacy_path).read_bytes()
    assert repeated["status"] == "unchanged"


def test_legacy_reconciliation_preserves_custom_anchor_instructions(tmp_path, monkeypatch):
    resources = _resources(tmp_path)
    monkeypatch.setattr("odibi_anchor._runtime_paths.resolve_resource_root", lambda: resources)
    target = tmp_path / "target"
    target.mkdir()
    legacy_path = ".assistant/agent_bootstrap.py"
    legacy_content = b"released legacy bootstrap\n"
    monkeypatch.setitem(
        module._LEGACY_MANAGED_HASHES, legacy_path, {module._sha256(legacy_content)}
    )
    custom = b"# Odibi Anchor operating contract\nCustom rules using agent_bootstrap.py\n"
    for relative, content in module._desired_files("databricks").items():
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if relative == legacy_path:
            content = legacy_content
        elif relative == ".assistant_instructions.md":
            content = custom
        destination.write_bytes(content)

    result = setup_host(target, adapter="databricks")
    repeated = setup_host(target, adapter="databricks")

    assert (target / ".assistant_instructions.md").read_bytes() == custom
    assert result["compatible_unmanaged_files"] == [".assistant_instructions.md"]
    assert repeated["status"] == "unchanged"
    assert repeated["compatible_unmanaged_files"] == [".assistant_instructions.md"]
    manifest = json.loads((target / module._MANIFEST).read_text())
    assert ".assistant_instructions.md" not in manifest["files"]


def test_legacy_reconciliation_refuses_unknown_distribution_bytes(tmp_path, monkeypatch):
    resources = _resources(tmp_path)
    monkeypatch.setattr("odibi_anchor._runtime_paths.resolve_resource_root", lambda: resources)
    target = tmp_path / "target"
    target.mkdir()
    legacy_path = ".assistant/agent_bootstrap.py"
    legacy_content = b"released legacy bootstrap\n"
    monkeypatch.setitem(
        module._LEGACY_MANAGED_HASHES, legacy_path, {module._sha256(legacy_content)}
    )
    for relative, content in module._desired_files("databricks").items():
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if relative == legacy_path:
            content = legacy_content
        elif relative.endswith("quick-reference.md"):
            content = b"unknown edit\n"
        destination.write_bytes(content)
    before = {path: path.read_bytes() for path in target.rglob("*") if path.is_file()}

    with pytest.raises(HostSetupError, match="unmanaged destination collision"):
        setup_host(target, adapter="databricks")

    assert {path: path.read_bytes() for path in target.rglob("*") if path.is_file()} == before


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


def test_claude_receives_native_skill_discovery_mirror(tmp_path, monkeypatch):
    resources = _resources(tmp_path)
    monkeypatch.setattr("odibi_anchor._runtime_paths.resolve_resource_root", lambda: resources)
    target = tmp_path / "claude"
    target.mkdir()

    result = setup_host(target, adapter="claude")

    canonical = target / ".assistant" / "skills" / "setting-up-odibi-anchor" / "SKILL.md"
    discovered = target / ".claude" / "skills" / "setting-up-odibi-anchor" / "SKILL.md"
    assert discovered.read_bytes() == canonical.read_bytes()
    assert any(
        item["path"] == ".claude/skills/setting-up-odibi-anchor/SKILL.md"
        for item in result["managed_files"]
    )


def test_databricks_installs_complete_authored_guidance_without_snapshot_cache(
    tmp_path, monkeypatch
):
    resources = _resources(tmp_path)
    monkeypatch.setattr("odibi_anchor._runtime_paths.resolve_resource_root", lambda: resources)
    target = tmp_path / "databricks"
    target.mkdir()

    result = setup_host(target, adapter="databricks")

    assert result["resource_profile"] == "databricks_workspace_compact"
    assert result["verified_skill_count"] == 18
    assert result["verified_file_count"] == len(result["managed_files"])
    assert result["omitted_packaged_prefixes"] == [
        ".assistant/references/snapshots/"
    ]
    assert (target / ".assistant" / "skills" / "debugging" / "SKILL.md").is_file()
    assert (
        target / ".assistant" / "references" / "odibi-anchor" / "workflow.md"
    ).is_file()
    installed_instructions = (target / ".assistant_instructions.md").read_text(
        encoding="utf-8"
    )
    installed_setup_skill = (
        target / ".assistant" / "skills" / "setting-up-odibi-anchor" / "SKILL.md"
    ).read_text(encoding="utf-8")
    assert "latest" in installed_instructions
    assert "latest" in installed_setup_skill
    assert "agent_bootstrap.py" in installed_instructions
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


@pytest.mark.parametrize("adapter", ["amp", "chatgpt", "claude", "databricks"])
def test_generated_python_bytecode_is_never_deployed(tmp_path, monkeypatch, adapter):
    resources = _resources(tmp_path)
    cache = resources / ".assistant" / "__pycache__"
    cache.mkdir()
    (cache / "agent_bootstrap.cpython-312.pyc").write_bytes(b"generated bytecode")
    (resources / ".assistant" / "generated.pyo").write_bytes(b"optimized bytecode")
    monkeypatch.setattr("odibi_anchor._runtime_paths.resolve_resource_root", lambda: resources)
    target = tmp_path / adapter
    target.mkdir()

    result = setup_host(target, adapter=adapter)

    managed_paths = {item["path"] for item in result["managed_files"]}
    assert not any("__pycache__" in Path(path).parts for path in managed_paths)
    assert not any(path.endswith((".pyc", ".pyo")) for path in managed_paths)
    assert not (target / ".assistant" / "__pycache__").exists()
    assert not (target / ".assistant" / "generated.pyo").exists()


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


def test_publication_verification_failure_restores_original(tmp_path, monkeypatch):
    resources = _resources(tmp_path)
    monkeypatch.setattr("odibi_anchor._runtime_paths.resolve_resource_root", lambda: resources)
    target = tmp_path / "target"
    target.mkdir()
    setup_host(target, adapter="amp")
    managed = target / ".assistant" / "README.md"
    original = managed.read_bytes()
    (resources / ".assistant" / "README.md").write_text("new packaged content\n")
    monkeypatch.setattr(
        module,
        "_verify_publication",
        lambda *_args: (_ for _ in ()).throw(HostSetupError("verification failed")),
    )

    with pytest.raises(HostSetupError, match="verification failed"):
        setup_host(target, adapter="amp")

    assert managed.read_bytes() == original
    assert not list(target.glob(".anchor-host-stage-*"))


def test_databricks_staging_residue_uses_workspace_api(monkeypatch):
    staging = Path("/Workspace/Users/test@example.invalid/.anchor-host-stage-residue")
    calls = []
    workspace = SimpleNamespace(
        delete=lambda **kwargs: calls.append(kwargs),
    )
    def failed_rmtree(_path, *, onerror):
        onerror(Path.rmdir, str(staging), (OSError, OSError("not empty"), None))

    monkeypatch.setattr(module.shutil, "rmtree", failed_rmtree)
    original_exists = Path.exists
    monkeypatch.setattr(
        Path,
        "exists",
        lambda path: True if path == staging else original_exists(path),
    )
    monkeypatch.setattr(module.importlib, "import_module", _fake_databricks_sdk(workspace))

    module._cleanup_staging(staging, "databricks")

    assert calls == [{
        "path": "/Users/test@example.invalid/.anchor-host-stage-residue",
        "recursive": True,
    }]


def test_databricks_staging_cleanup_failure_is_reported(monkeypatch):
    staging = Path("/Workspace/Users/test@example.invalid/.anchor-host-stage-residue")
    workspace = SimpleNamespace(
        delete=lambda **_kwargs: (_ for _ in ()).throw(PermissionError("denied")),
    )
    def failed_rmtree(_path, *, onerror):
        onerror(Path.rmdir, str(staging), (OSError, OSError("not empty"), None))

    monkeypatch.setattr(module.shutil, "rmtree", failed_rmtree)
    monkeypatch.setattr(Path, "exists", lambda path: path == staging)
    monkeypatch.setattr(module.importlib, "import_module", _fake_databricks_sdk(workspace))

    with pytest.raises(HostSetupError, match="Workspace staging cleanup failed"):
        module._cleanup_staging(staging, "databricks")


class WorkspaceNotFound(RuntimeError):
    status_code = 404
    error_code = "RESOURCE_DOES_NOT_EXIST"


class FakeWorkspaceFiles:
    def __init__(self, root: str) -> None:
        self.directories = {root}
        self.files: dict[str, bytes] = {}
        self.calls: list[tuple[str, str]] = []
        self.fail_upload_once: str | None = None
        self.metadata_overrides: dict[str, dict[str, Any]] = {}

    def list(self, path: str):
        self.calls.append(("list", path))
        if path not in self.directories:
            raise WorkspaceNotFound(path)
        entries = []
        for name in sorted(self.directories | set(self.files)):
            if Path(name).parent.as_posix() != path:
                continue
            is_file = name in self.files
            content = self.files.get(name, b"")
            metadata = {
                "path": name, "object_id": int(module._sha256(name.encode())[:12], 16),
                "object_type": "FILE" if is_file else "DIRECTORY",
                "size": len(content) if is_file else None,
                "modified_at": int(module._sha256(content)[:12], 16) if is_file else None,
                **self.metadata_overrides.get(name, {}),
            }
            entries.append(SimpleNamespace(**metadata))
        return iter(entries)

    def get_status(self, path: str):
        self.calls.append(("get_status", path))
        if path not in self.directories:
            raise WorkspaceNotFound(path)
        return SimpleNamespace(object_type=SimpleNamespace(value="DIRECTORY"))

    def download(self, path: str):
        self.calls.append(("download", path))
        if path not in self.files:
            raise WorkspaceNotFound(path)
        return io.BytesIO(self.files[path])

    def mkdirs(self, path: str) -> None:
        self.calls.append(("mkdirs", path))
        current = Path(path)
        for parent in reversed((current, *current.parents)):
            if parent.as_posix() != ".":
                self.directories.add(parent.as_posix())

    def upload(self, path: str, content: bytes, *, format, overwrite: bool) -> None:
        self.calls.append(("upload", path))
        assert format == "AUTO"
        assert overwrite is True
        if self.fail_upload_once == path:
            self.fail_upload_once = None
            raise OSError("simulated Workspace API write failure")
        if Path(path).parent.as_posix() not in self.directories:
            raise WorkspaceNotFound(f"missing parent for {path}")
        self.files[path] = bytes(content)

    def delete(self, path: str, *, recursive: bool = False) -> None:
        self.calls.append(("delete", path))
        if path in self.files:
            del self.files[path]
            return
        if recursive and path in self.directories:
            self.files = {
                name: content
                for name, content in self.files.items()
                if not name.startswith(path + "/")
            }
            self.directories = {
                name for name in self.directories if not name.startswith(path + "/")
            }
            return
        raise WorkspaceNotFound(path)


class SlowWorkspaceFiles(FakeWorkspaceFiles):
    """Workspace API fake with injected per-path latency and failure modes."""

    def __init__(self, root: str) -> None:
        super().__init__(root)
        self.latency: dict[str, float] = {}
        self.hang: str | None = None
        self.transport_timeout: str | None = None
        self.release = threading.Event()

    def download(self, path: str):
        if path == self.hang:
            self.release.wait(30)
        if path == self.transport_timeout:
            raise TimeoutError("Timed out after 0:01:00")
        time.sleep(self.latency.get(path, 0.0))
        return super().download(path)


def _workspace_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, configs: list | None = None,
) -> tuple[Path, SlowWorkspaceFiles]:
    resources = _resources(tmp_path)
    monkeypatch.setattr("odibi_anchor._runtime_paths.resolve_resource_root", lambda: resources)
    workspace = SlowWorkspaceFiles("/Users/test@example.invalid/anchor-host")
    monkeypatch.setattr(module.importlib, "import_module", _fake_databricks_sdk(workspace, configs))
    return resources, workspace


def test_databricks_workspace_install_uses_api_without_staging(tmp_path, monkeypatch):
    _resources_root, workspace = _workspace_setup(tmp_path, monkeypatch)
    monkeypatch.setattr(
        module.tempfile,
        "mkdtemp",
        lambda **_kwargs: pytest.fail("Workspace setup must not create FUSE staging"),
    )

    first = setup_host(
        "/Workspace/Users/test@example.invalid/anchor-host", adapter="databricks"
    )
    before_repeated = len(workspace.calls)
    repeated = setup_host(
        "/Workspace/Users/test@example.invalid/anchor-host", adapter="databricks"
    )
    repeated_calls = workspace.calls[before_repeated:]

    assert first["status"] == "installed"
    assert repeated["status"] == "unchanged"
    assert first["verified_skill_count"] == 18
    assert first["verified_file_count"] == len(first["managed_files"])
    assert sum(operation == "download" for operation, _path in repeated_calls) == (
        repeated["verified_file_count"] + 1
    )
    assert not any(".anchor-host-stage-" in path for _operation, path in workspace.calls)
    assert not any("__pycache__" in path or path.endswith(".pyc") for path in workspace.files)


def test_workspace_receipt_skips_content_and_reports_metadata(tmp_path, monkeypatch):
    _resources_root, workspace = _workspace_setup(tmp_path, monkeypatch)
    target = "/Workspace/Users/test@example.invalid/anchor-host"
    state = tmp_path / "local-state"
    first = setup_host(target, adapter="databricks", receipt_root=state)
    assert first["verification"] == "content"
    receipt_files = list(state.rglob("*.json"))
    assert len(receipt_files) == 1
    receipt = json.loads(receipt_files[0].read_text())
    assert receipt["binding"]["target_root"] == target
    assert receipt["binding"]["adapter"] == "databricks"
    assert set(path["path"] for path in first["managed_files"]) <= set(receipt["metadata"])
    assert module._MANIFEST in receipt["metadata"]
    before = dict(workspace.files)
    workspace.calls.clear()
    with recording() as recorder, phase("host_guidance"):
        repeated = setup_host(target, adapter="databricks", receipt_root=state)
    assert repeated["verification"] == "metadata_receipt"
    assert repeated["managed_files"] == first["managed_files"]
    assert not any(operation == "download" for operation, _ in workspace.calls)
    assert workspace.files == before
    read = recorder.summary()["phases"][0]["sub_phases"][1]
    assert read["verification"] == "metadata_receipt"
    assert read["bytes"] == 0
    list_calls = sum(operation == "list" for operation, _ in workspace.calls)
    content_calls = first["verified_file_count"] + 1
    assert 0 < list_calls < content_calls
    print(f"Workspace receipt: {content_calls} content reads -> {list_calls} lists; "
          f"at 50ms/call: {content_calls * 50}ms -> {list_calls * 50}ms API service time")


@pytest.mark.parametrize("change", [
    "object_id", "size", "modified_at", "unavailable", "extra", "corrupt_receipt",
    "version", "adapter", "target", "manifest_binding", "missing_receipt", "list_failure",
])
def test_workspace_receipt_falls_back_to_content(tmp_path, monkeypatch, change):
    _resources_root, workspace = _workspace_setup(tmp_path, monkeypatch)
    target = "/Workspace/Users/test@example.invalid/anchor-host"
    state = tmp_path / "state"
    setup_host(target, adapter="databricks", receipt_root=state)
    managed = "/Users/test@example.invalid/anchor-host/.assistant/README.md"
    receipt_file = next(state.rglob("*.json"))
    receipt = json.loads(receipt_file.read_text())
    if change in {"object_id", "size", "modified_at", "unavailable"}:
        field = "modified_at" if change == "unavailable" else change
        workspace.metadata_overrides[managed] = {field: None if change == "unavailable" else 999}
    elif change == "extra":
        workspace.files["/Users/test@example.invalid/anchor-host/.assistant/extra.md"] = b"extra"
    elif change == "corrupt_receipt":
        receipt_file.write_text("{")
    elif change == "missing_receipt":
        receipt_file.unlink()
    elif change == "list_failure":
        def unavailable_list(_path):
            raise TimeoutError("metadata unavailable")
        monkeypatch.setattr(workspace, "list", unavailable_list)
    else:
        field = {"version": "package_version", "adapter": "adapter", "target": "target_root",
                 "manifest_binding": "manifest_sha256"}[change]
        receipt["binding"][field] = "different"
        receipt_file.write_text(json.dumps(receipt))
    workspace.calls.clear()
    result = setup_host(target, adapter="databricks", receipt_root=state)
    assert result["verification"] == "content"
    assert sum(operation == "download" for operation, _ in workspace.calls) == result["verified_file_count"] + 1


@pytest.mark.parametrize("change", ["edit", "missing", "manifest"])
def test_workspace_receipt_does_not_hide_drift(tmp_path, monkeypatch, change):
    _resources_root, workspace = _workspace_setup(tmp_path, monkeypatch)
    target = "/Workspace/Users/test@example.invalid/anchor-host"
    state = tmp_path / "state"
    setup_host(target, adapter="databricks", receipt_root=state)
    managed = "/Users/test@example.invalid/anchor-host/.assistant/README.md"
    if change == "edit":
        workspace.files[managed] = b"unapproved edit"
    elif change == "manifest":
        workspace.files["/Users/test@example.invalid/anchor-host/" + module._MANIFEST] += b"\n"
    else:
        del workspace.files[managed]
    before = dict(workspace.files)
    with pytest.raises(HostSetupError) as caught:
        setup_host(target, adapter="databricks", receipt_root=state)
    if change == "manifest":
        assert "non-canonical" in str(caught.value)
    else:
        assert caught.value.error_code == "host_guidance_drift"
    assert workspace.files == before


def test_workspace_receipt_not_recorded_when_metadata_changes_during_verification(tmp_path, monkeypatch):
    _resources_root, workspace = _workspace_setup(tmp_path, monkeypatch)
    target = "/Workspace/Users/test@example.invalid/anchor-host"
    setup_host(target, adapter="databricks")
    state = tmp_path / "state"
    original = workspace.download

    def changing_download(path):
        stream = original(path)
        if path.endswith("/.assistant/README.md"):
            workspace.metadata_overrides[path] = {"modified_at": 999}
        return stream

    monkeypatch.setattr(workspace, "download", changing_download)
    assert setup_host(target, adapter="databricks", receipt_root=state)["verification"] == "content"
    assert not list(state.rglob("*.json"))


@pytest.mark.parametrize("receipt_root", ["/Workspace/cache", "/Volumes/cache", "/dbfs/cache", "relative"])
def test_workspace_receipts_are_never_stored_in_remote_or_relative_paths(tmp_path, monkeypatch, receipt_root):
    _resources_root, workspace = _workspace_setup(tmp_path, monkeypatch)
    target = "/Workspace/Users/test@example.invalid/anchor-host"
    setup_host(target, adapter="databricks", receipt_root=receipt_root)
    workspace.calls.clear()
    result = setup_host(target, adapter="databricks", receipt_root=receipt_root)
    assert result["verification"] == "content"
    assert not any(operation == "list" for operation, _ in workspace.calls)
    assert not any("receipt" in path for path in workspace.files)


def test_databricks_workspace_reads_are_bounded_concurrent_complete_and_ordered(
    tmp_path, monkeypatch,
):
    active = 0
    maximum = 0
    lock = threading.Lock()

    def read(_workspace, path):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.01)
        with lock:
            active -= 1
        return path.encode()

    monkeypatch.setattr(module, "_workspace_read", read)
    relative_paths = [f"guidance/{index:02d}.md" for index in range(24)]

    result = module._workspace_read_many(object(), tmp_path, relative_paths)

    assert list(result) == relative_paths
    last = result[relative_paths[-1]]
    assert last is not None
    assert last.endswith(relative_paths[-1].encode())
    assert 1 < maximum <= module._DATABRICKS_READ_WORKERS


def test_databricks_workspace_reconciles_legacy_files_and_preserves_custom_instructions(
    tmp_path, monkeypatch
):
    resources, workspace = _workspace_setup(tmp_path, monkeypatch)
    target = "/Workspace/Users/test@example.invalid/anchor-host"
    legacy_path = ".assistant/agent_bootstrap.py"
    legacy_content = b"released legacy bootstrap\n"
    monkeypatch.setitem(
        module._LEGACY_MANAGED_HASHES, legacy_path, {module._sha256(legacy_content)}
    )
    custom = b"# Odibi Anchor operating contract\nCustom rules using agent_bootstrap.py\n"
    for relative, content in module._desired_files("databricks").items():
        if relative == legacy_path:
            content = legacy_content
        elif relative == ".assistant_instructions.md":
            content = custom
        workspace.files[f"/Users/test@example.invalid/anchor-host/{relative}"] = content

    result = setup_host(target, adapter="databricks")
    repeated = setup_host(target, adapter="databricks")

    assert result["status"] == "upgraded"
    assert result["reconciled_legacy_managed_files"] == [legacy_path]
    assert result["compatible_unmanaged_files"] == [".assistant_instructions.md"]
    assert workspace.files[f"/Users/test@example.invalid/anchor-host/{legacy_path}"] == (
        resources / legacy_path
    ).read_bytes()
    assert workspace.files[
        "/Users/test@example.invalid/anchor-host/.assistant_instructions.md"
    ] == custom
    assert repeated["status"] == "unchanged"


def test_databricks_workspace_api_upgrade_restores_original_on_failure(
    tmp_path, monkeypatch
):
    resources, workspace = _workspace_setup(tmp_path, monkeypatch)
    target = "/Workspace/Users/test@example.invalid/anchor-host"
    setup_host(target, adapter="databricks")
    original = dict(workspace.files)
    (resources / ".assistant" / "README.md").write_text("updated guidance\n")
    (resources / "agent_bootstrap.py").write_text("# updated bootstrap\n")
    failing_path = "/Users/test@example.invalid/anchor-host/agent_bootstrap.py"
    workspace.fail_upload_once = failing_path

    with pytest.raises(HostSetupError, match="publication failed; original state restored"):
        setup_host(target, adapter="databricks")

    assert workspace.files == original
    assert not any(".anchor-host-stage-" in path for _operation, path in workspace.calls)


def test_databricks_workspace_api_refuses_modified_managed_file(tmp_path, monkeypatch):
    _resources_root, workspace = _workspace_setup(tmp_path, monkeypatch)
    target = "/Workspace/Users/test@example.invalid/anchor-host"
    setup_host(target, adapter="databricks")
    managed = "/Users/test@example.invalid/anchor-host/.assistant/README.md"
    workspace.files[managed] = b"user edit\n"
    before = dict(workspace.files)

    with pytest.raises(HostSetupError, match="modified managed file"):
        setup_host(target, adapter="databricks")

    assert workspace.files == before


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


def test_databricks_workspace_client_is_explicitly_bounded_and_reused(tmp_path, monkeypatch):
    configs: list = []
    _workspace_setup(tmp_path, monkeypatch, configs)
    monkeypatch.setenv("ANCHOR_WORKSPACE_API_TIMEOUT_SECONDS", "7")
    target = "/Workspace/Users/test@example.invalid/anchor-host"

    setup_host(target, adapter="databricks")
    setup_host(target, adapter="databricks")

    assert configs == [{"http_timeout_seconds": 7, "retry_timeout_seconds": 14}]


@pytest.mark.parametrize("failure", ["hanging_read", "transport_timeout"])
def test_databricks_workspace_read_timeout_is_structured_and_fail_closed(
    tmp_path, monkeypatch, failure
):
    _resources_root, workspace = _workspace_setup(tmp_path, monkeypatch)
    target = "/Workspace/Users/test@example.invalid/anchor-host"
    setup_host(target, adapter="databricks")
    stalled = "/Users/test@example.invalid/anchor-host/.assistant/README.md"
    for path in workspace.files:
        workspace.latency[path] = 0.005
    setattr(workspace, "hang" if failure == "hanging_read" else "transport_timeout", stalled)
    monkeypatch.setenv("ANCHOR_WORKSPACE_API_TIMEOUT_SECONDS", "0.1")
    before = dict(workspace.files)
    writes_before = sum(operation == "upload" for operation, _path in workspace.calls)

    started = time.perf_counter()
    try:
        with recording() as recorder, pytest.raises(BootstrapPhaseTimeout) as raised, phase("host_guidance"):
            setup_host(target, adapter="databricks")
    finally:
        workspace.release.set()
    elapsed = time.perf_counter() - started

    error: Any = raised.value
    assert error.error_code == "bootstrap_phase_timeout"
    assert error.context["phase"] == "host_guidance"
    assert error.context["layer"] == "workspace_api"
    assert error.context["item"] == stalled
    assert error.context["setting"] == "ANCHOR_WORKSPACE_API_TIMEOUT_SECONDS"
    if failure == "hanging_read":
        assert error.context["sub_phase"] == "read"
        assert error.context["limit_seconds"] == pytest.approx(0.3)
        assert error.context["elapsed_ms"] >= 300
        assert elapsed < 5
    assert "ANCHOR_WORKSPACE_API_TIMEOUT_SECONDS" in error.copy_ready
    assert workspace.files == before
    assert sum(operation == "upload" for operation, _path in workspace.calls) == writes_before
    [host_phase] = recorder.summary()["phases"]
    assert host_phase["outcome"] == "timeout"
    assert [(entry["phase"], entry["outcome"]) for entry in host_phase["sub_phases"]] == [
        ("package_resources", "ok"), ("read", "timeout"),
    ]


def test_databricks_guidance_timings_report_sub_phases_and_slowest_files(tmp_path, monkeypatch):
    resources, workspace = _workspace_setup(tmp_path, monkeypatch)
    target = "/Workspace/Users/test@example.invalid/anchor-host"
    setup_host(target, adapter="databricks")
    slow = ".assistant/README.md"
    (resources / slow).write_text("updated guidance for a timed upgrade\n")
    workspace.latency[f"/Users/test@example.invalid/anchor-host/{slow}"] = 0.05

    with recording() as recorder, phase("host_guidance"):
        upgraded = setup_host(target, adapter="databricks")
    with recording() as repeated_recorder, phase("host_guidance"):
        repeated = setup_host(target, adapter="databricks")

    assert upgraded["status"] == "upgraded"
    assert repeated == setup_host(target, adapter="databricks")
    [host_phase] = recorder.summary()["phases"]
    assert [(entry["phase"], entry["outcome"]) for entry in host_phase["sub_phases"]] == [
        ("package_resources", "ok"), ("read", "ok"), ("verify", "ok"), ("publish", "ok"),
    ]
    read = host_phase["sub_phases"][1]
    assert read["layer"] == "workspace_api"
    assert read["file_count"] == upgraded["verified_file_count"] + 1
    slowest = read["slowest_files"]
    assert len(slowest) == 5
    assert slowest[0]["path"] == slow
    assert slowest[0]["elapsed_ms"] >= 50
    # Reconciliation reads the previously published bytes before upgrading them.
    assert slowest[0]["bytes"] == len((ROOT / slow).read_bytes())
    [repeated_phase] = repeated_recorder.summary()["phases"]
    assert repeated_phase["sub_phases"][-1] == {
        "phase": "publish", "elapsed_ms": 0.0, "outcome": "not_required",
        "reason": "managed files unchanged",
    }
