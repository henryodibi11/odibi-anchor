"""Observable host layout, migration and crash recovery contracts."""

import ast
import hashlib
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
@pytest.mark.parametrize("adapter", ["amp", "chatgpt", "claude"])
def test_one_launcher_and_cwd_independent_discovery(tmp_path, monkeypatch, custom, adapter):
    root = tmp_path / "host"
    root.mkdir()
    home = tmp_path / "owner-chosen" if custom else root / "anchor"
    first = setup_host(root, adapter=adapter, home=home)
    monkeypatch.chdir(tmp_path)
    second = setup_host(root, adapter=adapter)
    assert first["host_home"] == str(home)
    assert second["status"] == "unchanged"
    assert list(root.rglob("agent_bootstrap.py")) == ([] if custom else [home / "agent_bootstrap.py"])
    assert (home / "agent_bootstrap.py").is_file()
    assert not (root / ".odibi-anchor-host-guidance.json").exists()
    assert "Canonical bootstrap" in (root / ".assistant_instructions.md").read_text()
    assert "mandatory agent execution contract" not in (root / ".assistant_instructions.md").read_text()
    assert (root / ("CLAUDE.md" if adapter == "claude" else "AGENTS.md")).is_file()


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


@pytest.mark.parametrize("custom", [False, True])
def test_databricks_native_contract_is_inline_with_canonical_home(tmp_path, monkeypatch, custom):
    from tests.test_host_setup import FakeWorkspaceFiles, _fake_databricks_sdk

    legacy = importlib.import_module("odibi_anchor.host_setup")
    root = Path("/Workspace/Users/person@example.invalid")
    home = root / ('owner tools/Ancrage é "chosen"' if custom else "anchor")
    workspace = FakeWorkspaceFiles("/Users/person@example.invalid")
    monkeypatch.setattr(legacy.importlib, "import_module", _fake_databricks_sdk(workspace))
    setup_host(root, adapter="databricks", home=home, receipt_root=tmp_path)
    api_root = str(root).removeprefix("/Workspace")
    api_home = str(home).removeprefix("/Workspace")
    native = workspace.files[api_root + "/.assistant_instructions.md"].decode()
    marker, contract = native.split("\n", 1)
    assert json.loads(marker.removeprefix(host_home.MARKER).removesuffix(" -->")) == str(home)
    assert contract == workspace.files[api_home + "/instructions.md"].decode()
    assert "mandatory agent execution contract" in contract
    assert "<instruction-root>/.assistant/agent_bootstrap.py" not in contract
    assert json.dumps(str(home / "agent_bootstrap.py"), ensure_ascii=False) in contract
    example = ast.parse(contract.split("```python\n", 1)[1].split("```", 1)[0])
    bootstrap_call = next(
        node for node in ast.walk(example)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "run_path"
    )
    assert ast.literal_eval(bootstrap_call.args[0]) == str(home / "agent_bootstrap.py")
    assert len(native) <= 20_000
    assert host_home.host_launcher(root, adapter="databricks") == home / "agent_bootstrap.py"
    assert setup_host(root, adapter="databricks", receipt_root=tmp_path)["status"] == "unchanged"


@pytest.mark.parametrize("previous_layout", ["legacy", "0.4.1"])
def test_databricks_inline_upgrade_plan_repeat_and_rollback(tmp_path, monkeypatch, previous_layout):
    from tests.test_host_setup import FakeWorkspaceFiles, _fake_databricks_sdk

    legacy = importlib.import_module("odibi_anchor.host_setup")
    root = Path("/Workspace/Users/test@example.invalid")
    home = root / "owner-selected-home"
    workspace = FakeWorkspaceFiles("/Users/test@example.invalid")
    monkeypatch.setattr(legacy.importlib, "import_module", _fake_databricks_sdk(workspace))
    native = "/Users/test@example.invalid/.assistant_instructions.md"
    if previous_layout == "legacy":
        _setup_legacy_host(root, adapter="databricks")
    else:
        setup_host(root, adapter="databricks", home=home, receipt_root=tmp_path)
        # Released 0.4.1's managed pointer and ownership hash, not owner drift.
        pointer = (
            host_home.MARKER + json.dumps(str(home)) + " -->\n"
            f"Read and follow `{home}/instructions.md` before substantive work.\n"
            f"Canonical bootstrap: `{home}/agent_bootstrap.py`.\n"
        ).encode()
        workspace.files[native] = pointer
        manifest_path = str(home / "guidance.json").removeprefix("/Workspace")
        manifest = json.loads(workspace.files[manifest_path])
        manifest["files"][str(root / ".assistant_instructions.md")] = hashlib.sha256(pointer).hexdigest()
        workspace.files[manifest_path] = json.dumps(manifest).encode()
    before = dict(workspace.files)
    plan = setup_host(root, adapter="databricks", home=home, reconcile=True, receipt_root=tmp_path)
    assert plan["changed_files"]
    assert workspace.files == before
    setup_host(root, adapter="databricks", home=home, reconcile=True, dry_run=False, receipt_root=tmp_path)
    assert "mandatory agent execution contract" in workspace.files[native].decode()
    assert setup_host(root, adapter="databricks", receipt_root=tmp_path)["status"] == "unchanged"
    assert setup_host(root, adapter="databricks", home=home, rollback=True)["status"] == "rolled_back"
    # The single-use rollback receipt is consumed, not restored as another undo.
    receipt_path = str(home / "last-migration.json").removeprefix("/Workspace")
    restorable = {path: content for path, content in before.items() if path != receipt_path}
    assert {path: workspace.files.get(path) for path in restorable} == restorable
    assert not any(path.endswith(("pending.json", ".anchor-host-migration.json")) for path in workspace.files)


@pytest.mark.parametrize("characters", [19_999, 20_000, 20_001])
def test_databricks_native_character_limit_precedes_publication(tmp_path, monkeypatch, characters):
    root = tmp_path / "host"
    root.mkdir()
    home = root / "anchor"
    legacy = importlib.import_module("odibi_anchor.host_setup")
    desired_files = legacy._desired_files
    marker = host_home.MARKER + json.dumps(str(home)) + " -->\n"

    def sized_contract(adapter):
        files = desired_files(adapter)
        # UTF-8 bytes exceed the character limit even for accepted cases.
        files[".assistant_instructions.md"] = ("é" * (characters - len(marker))).encode()
        return files

    monkeypatch.setattr(legacy, "_desired_files", sized_contract)
    if characters <= 20_000:
        setup_host(root, adapter="databricks")
        native = (root / ".assistant_instructions.md").read_text()
        assert len(native) == characters
    else:
        with pytest.raises(HostSetupError, match=r"20001.*20000"):
            setup_host(root, adapter="databricks", reconcile=True, dry_run=False)
        assert list(root.iterdir()) == []


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
        dbroot = base / "databricks"
        dbroot.mkdir()
        dbhome = dbroot / "chosen home"
        setup_host(dbroot, adapter="databricks", home=dbhome)
        native = (dbroot / ".assistant_instructions.md").read_text()
        assert native.split("\n", 1)[1] == (dbhome / "instructions.md").read_text()
        assert "mandatory agent execution contract" in native
        assert str(dbhome / "agent_bootstrap.py") in native
        assert len(native) <= 20000
        assert setup_host(dbroot, adapter="databricks")["status"] == "unchanged"
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


def test_recorded_instruction_root_matches_as_a_path_including_windows_forms():
    """Bindings record as_posix() roots; Windows setup compares against backslash roots."""
    from pathlib import PurePosixPath, PureWindowsPath

    root = PureWindowsPath(r"C:\Users\owner\.odibi-anchor\host")
    assert host_home._binding_root_matches("C:/Users/owner/.odibi-anchor/host", root)
    assert host_home._binding_root_matches(r"C:\Users\owner\.odibi-anchor\host", root)
    for other in ("C:/Users/owner/.odibi-anchor/other", "C:/Users/owner/.odibi-anchor",
                  "D:/Users/owner/.odibi-anchor/host", "Users/owner/.odibi-anchor/host", "", None, 1):
        assert not host_home._binding_root_matches(other, root), other
    posix = PurePosixPath("/home/owner/host")
    assert host_home._binding_root_matches("/home/owner/host/", posix)
    assert not host_home._binding_root_matches("/home/owner/other", posix)


@pytest.mark.parametrize("variant", ["unchanged", "equivalent_spelling", "other_root", "extra_key", "non_canonical"])
def test_reconcile_accepts_path_equivalent_binding_and_refuses_foreign_ones(tmp_path, variant):
    from odibi_anchor.host_setup import _BINDING, _canonical_json, read_host_binding
    from odibi_anchor.portfolio import write_portfolio

    root = tmp_path / "host"
    root.mkdir()
    (tmp_path / "other").mkdir()
    (tmp_path / "target").mkdir()
    external = tmp_path / "external.toml"
    write_portfolio(external, {
        "schema_version": 1,
        "authority": {"id": "owner", "trust_domain": "work"},
        "hosts": {"local": {"adapter": "amp", "instruction_root": str(root),
                            "local_state_root": str(tmp_path / "state")}},
        "projects": {"alpha": {"targets": {"local": str(tmp_path / "target")}}},
        "personas": {},
    })
    _setup_legacy_host(root, adapter="amp", portfolio_config=external, host_id="local")
    setup_host(root, adapter="amp")
    binding_path = root / "anchor" / _BINDING
    written = binding_path.read_bytes()
    binding = json.loads(written)
    if variant == "equivalent_spelling":
        binding["instruction_root"] += "/"  # same path, different text, like C:/x versus C:\x
    elif variant == "other_root":
        binding["instruction_root"] = str(tmp_path / "other")
    elif variant == "extra_key":
        binding["extra"] = "value"
    content = _canonical_json(binding)
    binding_path.write_bytes(content.rstrip(b"\n") if variant == "non_canonical" else content)
    before = {str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}

    if variant == "unchanged":
        # Anchor's own binding records an as_posix() root, exactly as on Windows. The
        # `--reconcile` plan and its apply both accept it without rewriting it.
        assert setup_host(root, adapter="amp", reconcile=True, dry_run=True)["status"] == "unchanged"
        setup_host(root, adapter="amp", reconcile=True, dry_run=False)
        reread = read_host_binding(root)
        assert (reread["host_id"], reread["config_path"]) == ("local", str(root / "anchor/anchor.toml"))
        assert (root / "anchor/anchor.toml").read_bytes() == external.read_bytes()
        assert binding_path.read_bytes() == written
        return
    if variant == "equivalent_spelling":
        # Same path, different bytes: ownership passes, but bytes Anchor did not write
        # still need explicit owner approval before any replacement.
        assert setup_host(root, adapter="amp", reconcile=True, dry_run=True)["status"] == "planned"
        with pytest.raises(HostSetupError, match="host migration conflict"):
            setup_host(root, adapter="amp", reconcile=True, dry_run=False)
    else:
        with pytest.raises(HostSetupError, match="host binding ownership/schema mismatch"):
            setup_host(root, adapter="amp", reconcile=True, dry_run=True)
    assert {str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before
