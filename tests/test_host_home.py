"""Observable host layout, migration and crash recovery contracts."""

import importlib
import json
import os
import runpy
import shutil
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest

from odibi_anchor import host_home
from odibi_anchor.host_setup import HostSetupError, _setup_legacy_host, setup_host


@pytest.fixture(autouse=True)
def current_host_modules():
    # Earlier bootstrap tests evict package modules; patch/catch the same live
    # module objects that the public setup gateway will resolve in this test.
    global host_home, HostSetupError, _setup_legacy_host, setup_host
    host_home = importlib.import_module("odibi_anchor.host_home")
    setup = importlib.import_module("odibi_anchor.host_setup")
    HostSetupError = setup.HostSetupError
    _setup_legacy_host = setup._setup_legacy_host
    setup_host = setup.setup_host


@pytest.mark.parametrize("custom", [False, True])
def test_one_launcher_and_cwd_independent_discovery(tmp_path, monkeypatch, custom):
    root = tmp_path / "host"
    root.mkdir()
    home = tmp_path / "owner-chosen" if custom else root / "anchor"
    first = setup_host(root, adapter="amp", home=home)
    monkeypatch.chdir(tmp_path)
    second = setup_host(root, adapter="amp")
    assert first["host_home"] == str(home)
    assert second["status"] == "unchanged"
    assert list(root.rglob("agent_bootstrap.py")) == ([] if custom else [home / "agent_bootstrap.py"])
    assert (home / "agent_bootstrap.py").is_file()
    assert not (root / ".odibi-anchor-host-guidance.json").exists()
    assert "Canonical bootstrap" in (root / ".assistant_instructions.md").read_text()
    assert (root / "AGENTS.md").is_file()


def test_legacy_migration_preserves_project_state_and_native_skills(tmp_path):
    root = tmp_path / "host"
    root.mkdir()
    _setup_legacy_host(root, adapter="claude")
    state = root / ".odibi-anchor" / "workspace" / "projects" / "p" / "PROJECT.md"
    state.parent.mkdir(parents=True)
    state.write_text("owner data")
    result = setup_host(root, adapter="claude", home=tmp_path / "chosen")
    assert state.read_text() == "owner data"
    assert not (root / ".assistant/agent_bootstrap.py").exists()
    assert (root / ".claude/skills").is_dir()
    assert Path(result["backup_path"]).is_relative_to(tmp_path / "chosen")
    assert setup_host(root, adapter="claude")["status"] == "unchanged"


def test_conflict_precedes_any_publication(tmp_path):
    root = tmp_path / "host"
    root.mkdir()
    (root / "agent_bootstrap.py").write_text("owner's program")
    with pytest.raises(HostSetupError, match="conflict"):
        setup_host(root, adapter="amp")
    assert not (root / "anchor").exists()
    assert (root / "agent_bootstrap.py").read_text() == "owner's program"


def test_crash_pending_refuses_and_explicit_rollback_restores(tmp_path, monkeypatch):
    root = tmp_path / "host"
    root.mkdir()
    original_io = host_home._io
    count = 0

    def crashing_io(adapter):
        read, write = original_io(adapter)

        def crashing_write(path, content):
            nonlocal count
            write(path, content)
            if (root / "anchor/pending.json").exists() and path.name != "pending.json":
                count += 1
                if count == 3:
                    raise KeyboardInterrupt("simulated process death")

        return read, crashing_write

    monkeypatch.setattr(host_home, "_io", crashing_io)
    with pytest.raises(KeyboardInterrupt):
        setup_host(root, adapter="amp")
    monkeypatch.setattr(host_home, "_io", original_io)
    with pytest.raises(HostSetupError, match="pending") as caught:
        setup_host(root, adapter="amp")
    assert caught.value.error_code == "host_migration_pending"
    assert setup_host(root, adapter="amp", rollback=True)["status"] == "rolled_back"
    assert not (root / "AGENTS.md").exists()
    assert setup_host(root, adapter="amp")["status"] == "installed"


def test_rollback_preserves_concurrent_edit(tmp_path, monkeypatch):
    root = tmp_path / "host"
    root.mkdir()
    original_io = host_home._io

    def crashing_io(adapter):
        read, write = original_io(adapter)

        def crashing_write(path, content):
            write(path, content)
            if path.name == "AGENTS.md":
                raise KeyboardInterrupt

        return read, crashing_write

    monkeypatch.setattr(host_home, "_io", crashing_io)
    with pytest.raises(KeyboardInterrupt):
        setup_host(root, adapter="amp")
    monkeypatch.setattr(host_home, "_io", original_io)
    (root / "AGENTS.md").write_text("concurrent owner edit")
    with pytest.raises(HostSetupError, match="rollback conflict"):
        setup_host(root, adapter="amp", rollback=True)
    assert (root / "AGENTS.md").read_text() == "concurrent owner edit"
    assert json.loads((root / "anchor/pending.json").read_bytes())["files"]


def test_successful_migration_can_roll_back_with_verified_backups(tmp_path):
    root = tmp_path / "host"
    root.mkdir()
    _setup_legacy_host(root, adapter="amp")
    (root / ".assistant/README.md").write_text("owner edit to back up")
    repaired = _setup_legacy_host(root, adapter="amp", reconcile=True, dry_run=False, approve_replace_edited=True)
    old_backup = Path(repaired["backup"]["root"])
    (old_backup / "owner-notes.txt").write_text("unknown backup content stays put")
    original = {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    result = setup_host(root, adapter="amp")
    assert (old_backup / "owner-notes.txt").read_text() == "unknown backup content stays put"
    assert not (old_backup / "BACKUP.json").exists()
    assert (root / "anchor/backups/legacy" / old_backup.name / "BACKUP.json").is_file()
    assert setup_host(root, adapter="amp", rollback=True)["status"] == "rolled_back"
    restored = {
        str(path.relative_to(root)): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and not path.is_relative_to(root / "anchor")
    }
    assert restored == original
    assert Path(result["backup_path"]).is_dir()


@pytest.mark.parametrize("change", ["content", "missing", "metadata_unavailable", "package"])
def test_databricks_receipt_never_hides_guidance_change(tmp_path, monkeypatch, change):
    import odibi_anchor
    import odibi_anchor.host_setup as legacy
    from tests.test_host_setup import FakeWorkspaceFiles, _fake_databricks_sdk

    root = Path("/Workspace/Users/test@example.invalid/host")
    workspace = FakeWorkspaceFiles("/Users/test@example.invalid/host")
    monkeypatch.setattr(legacy.importlib, "import_module", _fake_databricks_sdk(workspace))
    receipt = tmp_path / "compute-cache"
    first = setup_host(root, adapter="databricks", receipt_root=receipt)
    setup_host(root, adapter="databricks", receipt_root=receipt)
    warm = setup_host(root, adapter="databricks", receipt_root=receipt)
    assert warm["verification"] == "metadata_receipt"
    assert warm["status"] == "unchanged"
    path = "/Users/test@example.invalid/host/anchor/agent_bootstrap.py"
    assert path in workspace.files
    assert "/Users/test@example.invalid/host/agent_bootstrap.py" not in workspace.files
    assert "/Users/test@example.invalid/host/.assistant/agent_bootstrap.py" not in workspace.files
    if change == "content":
        workspace.files[path] = b"x" + workspace.files[path][1:]
        with pytest.raises(HostSetupError, match="conflict"):
            setup_host(root, adapter="databricks", receipt_root=receipt)
    else:
        if change == "missing":
            del workspace.files[path]
        elif change == "metadata_unavailable":
            workspace.metadata_overrides[path] = {"modified_at": None}
        else:
            monkeypatch.setattr(odibi_anchor, "__version__", "9.9.9")
        result = setup_host(root, adapter="databricks", receipt_root=receipt)
        assert result["verification"] == "content"
        assert path in workspace.files
    assert first["host_home"] == str(root / "anchor")


def test_installed_candidate_migrates_then_bootstraps_from_unrelated_cwd(tmp_path):
    repository = Path(__file__).resolve().parents[1]
    wheels = tmp_path / "wheels"
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(("ANCHOR_", "DATABRICKS_", "CW_SLACK_", "_CW_"))
        and name not in {"PYTHONPATH", "PYTHONHOME"}
    }
    environment.update(HOME=str(tmp_path / "user"), PYTHONNOUSERSITE="1")
    subprocess.run(
        [sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation", "-w", str(wheels), str(repository)],
        check=True,
        capture_output=True,
        env=environment,
    )
    venv = tmp_path / "venv"
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True, capture_output=True, env=environment)
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    subprocess.run(
        [str(python), "-m", "pip", "install", "--no-index", "--no-deps", str(next(wheels.glob("*.whl")))],
        check=True,
        capture_output=True,
        env=environment,
    )
    script = r"""
        import json, os, runpy, sys
        from pathlib import Path
        import odibi_anchor
        from odibi_anchor.host_setup import _setup_legacy_host, setup_host
        from odibi_anchor.portfolio import write_portfolio
        base = Path(sys.argv[1])
        root, target, state, chosen = [base / name for name in ("host", "project", "live-state", "chosen-home")]
        root.mkdir(); target.mkdir()
        _setup_legacy_host(root, adapter="amp")
        config = root / ".odibi-anchor/anchor.toml"
        config.parent.mkdir()
        write_portfolio(config, {
            "schema_version": 1, "authority": {"id": "owner", "trust_domain": "work"},
            "hosts": {
                "other": {"adapter": "amp", "instruction_root": str(root), "local_state_root": str(base / "wrong-state")},
                "local": {"adapter": "amp", "instruction_root": str(root), "local_state_root": str(state)}},
            "projects": {"alpha": {"targets": {"local": str(target)}}}, "personas": {}})
        _setup_legacy_host(root, adapter="amp", portfolio_config=config, host_id="local")
        original = config.read_bytes()
        setup_host(root, adapter="amp", home=chosen)
        setup_host(root, adapter="amp")
        assert not config.exists()
        assert (chosen / "anchor.toml").read_bytes() == original
        assert not (root / ".assistant/agent_bootstrap.py").exists()
        os.chdir(base)
        result = runpy.run_path(str(chosen / "agent_bootstrap.py"), init_globals={"ANCHOR_PROJECT_ID": "alpha"})
        assert result["STARTUP_PACKET"]["status"] == "ready"
        assert result["ROOT"] == str(target)
        assert Path(odibi_anchor.__file__).is_relative_to(base / "venv")
        assert result["BOOTSTRAP"]["state_home"] == str(state)
        print("INSTALLED_HOST_HOME_OK")
    """
    result = subprocess.run(
        [str(python), "-c", textwrap.dedent(script), str(tmp_path)],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "INSTALLED_HOST_HOME_OK" in result.stdout


@pytest.mark.parametrize("damage", ["copied", "identity_missing", "pending_before_identity"])
def test_canonical_launcher_refuses_copied_or_incomplete_home(tmp_path, damage):
    root = tmp_path / "host"
    root.mkdir()
    setup_host(root, adapter="amp")
    home = root / "anchor"
    if damage == "copied":
        copied = tmp_path / "copied"
        shutil.copytree(home, copied)
        home = copied
        message = "ownership mismatch"
    else:
        (home / "host.json").unlink()
        message = "identity is missing"
        if damage == "pending_before_identity":
            (home / "pending.json").write_text("{}")
            message = "migration pending"
    with pytest.raises(RuntimeError, match=message):
        runpy.run_path(str(home / "agent_bootstrap.py"))


def test_rollback_rechecks_destination_after_prevalidation(tmp_path, monkeypatch):
    root = tmp_path / "host"
    root.mkdir()
    setup_host(root, adapter="amp")
    original_io = host_home._io

    def concurrent_io(adapter):
        read, write = original_io(adapter)

        def concurrent_write(path, content):
            write(path, content)
            if path.name == "pending.json":
                (root / "AGENTS.md").write_text("owner edit during rollback")

        return read, concurrent_write

    monkeypatch.setattr(host_home, "_io", concurrent_io)
    with pytest.raises(HostSetupError, match="rollback conflict"):
        setup_host(root, adapter="amp", rollback=True)
    assert (root / "AGENTS.md").read_text() == "owner edit during rollback"
    assert (root / "anchor/pending.json").exists()


def test_databricks_mutation_timeout_preserves_journal_without_racing_rollback(monkeypatch):
    import odibi_anchor.host_setup as legacy
    from tests.test_host_setup import FakeWorkspaceFiles, _fake_databricks_sdk

    root = Path("/Workspace/Users/test@example.invalid/host")
    workspace = FakeWorkspaceFiles("/Users/test@example.invalid/host")
    release, finished = threading.Event(), threading.Event()
    upload = workspace.upload

    def slow_upload(path, content, **kwargs):
        if path.endswith("/.assistant/README.md"):
            release.wait(3)
            try:
                upload(path, content, **kwargs)
            finally:
                finished.set()
        else:
            upload(path, content, **kwargs)

    monkeypatch.setattr(workspace, "upload", slow_upload)
    monkeypatch.setattr(legacy.importlib, "import_module", _fake_databricks_sdk(workspace))
    monkeypatch.setenv("ANCHOR_WORKSPACE_API_TIMEOUT_SECONDS", "0.1")
    try:
        with pytest.raises(legacy.BootstrapPhaseTimeout) as caught:
            setup_host(root, adapter="databricks")
        assert caught.value.context["mutation_outcome"] == "unknown"
        assert caught.value.next_operation["retry_safety"] == "inspect_before_retry"
        assert "/Users/test@example.invalid/host/anchor/pending.json" in workspace.files
        assert not any(operation == "delete" for operation, _ in workspace.calls)
    finally:
        release.set()
        assert finished.wait(3)
    with pytest.raises(HostSetupError, match="pending"):
        setup_host(root, adapter="databricks")
    assert setup_host(root, adapter="databricks", rollback=True)["status"] == "rolled_back"


@pytest.mark.parametrize("selection", ["external_binding", "missing_explicit", "binding_conflict"])
def test_migration_preserves_portfolio_selection(tmp_path, selection):
    from odibi_anchor.host_setup import read_host_binding
    from odibi_anchor.portfolio import write_portfolio

    root = tmp_path / "host"
    root.mkdir()
    target = tmp_path / "target"
    target.mkdir()
    external = tmp_path / "external.toml"
    portfolio = {
        "schema_version": 1,
        "authority": {"id": "owner", "trust_domain": "work"},
        "hosts": {
            "other": {"adapter": "amp", "instruction_root": str(root), "local_state_root": str(tmp_path / "other")},
            "local": {"adapter": "amp", "instruction_root": str(root), "local_state_root": str(tmp_path / "state")}
        },
        "projects": {"alpha": {"targets": {"local": str(target)}}},
        "personas": {},
    }
    write_portfolio(external, portfolio)
    legacy_config = root / ".odibi-anchor/anchor.toml"
    if selection == "missing_explicit":
        write_portfolio(legacy_config, portfolio)
        _setup_legacy_host(root, adapter="amp", portfolio_config=legacy_config, host_id="local")
    else:
        _setup_legacy_host(root, adapter="amp", portfolio_config=external, host_id="local")
        if selection == "binding_conflict":
            write_portfolio(legacy_config, portfolio)
    before = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    if selection == "external_binding":
        setup_host(root, adapter="amp")
        setup_host(root, adapter="amp")
        assert (root / "anchor/anchor.toml").read_bytes() == external.read_bytes()
        assert read_host_binding(root)["host_id"] == "local"
        assert read_host_binding(root)["config_path"] == str(root / "anchor/anchor.toml")
    else:
        with pytest.raises(
            HostSetupError, match=r"explicit portfolio does not exist|binding and default portfolio disagree"
        ):
            setup_host(
                root,
                adapter="amp",
                **({"portfolio_config": tmp_path / "missing.toml"} if selection == "missing_explicit" else {}),
            )
        assert {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()} == before
    assert external.is_file()


@pytest.mark.parametrize("crash", ["after_locator", "after_journal", "during_rollback"])
def test_legacy_entrypoint_cannot_bypass_custom_home_transaction(tmp_path, monkeypatch, crash):
    root, home = tmp_path / "host", tmp_path / "custom-home"
    root.mkdir()
    _setup_legacy_host(root, adapter="amp")
    original_io = host_home._io
    if crash == "during_rollback":
        setup_host(root, adapter="amp", home=home)

    def crashing_io(adapter):
        read, write = original_io(adapter)

        def crashing_write(path, content):
            write(path, content)
            if (
                (crash == "after_locator" and path.name == host_home.TRANSACTION)
                or (crash == "after_journal" and path.name == "pending.json")
                or (crash == "during_rollback" and path == root / ".assistant/agent_bootstrap.py")
            ):
                raise KeyboardInterrupt

        return read, crashing_write

    monkeypatch.setattr(host_home, "_io", crashing_io)
    with pytest.raises(KeyboardInterrupt):
        setup_host(root, adapter="amp", home=home, rollback=crash == "during_rollback")
    monkeypatch.setattr(host_home, "_io", original_io)
    with pytest.raises(RuntimeError, match="migration pending"):
        runpy.run_path(str(root / ".assistant/agent_bootstrap.py"))
    with pytest.raises(HostSetupError, match="migration pending"):
        host_home.host_launcher(root, "amp")
    assert setup_host(root, adapter="amp", rollback=True)["status"] == "rolled_back"
    assert not (root / host_home.TRANSACTION).exists()
