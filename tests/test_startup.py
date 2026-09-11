import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import odibi_anchor.startup as startup_module
from odibi_anchor.legacy_import import apply_legacy_import, plan_legacy_import
from odibi_anchor.portfolio import write_portfolio
from odibi_anchor.startup import (
    doctor,
    install_guidance,
    launch,
    prepare_portfolio_runtime,
    register_project,
)


def test_launch_binds_exact_route_and_returns_callable(tmp_path, monkeypatch):
    home = tmp_path / "home"
    target = tmp_path / "target"
    artifact = home / "workspace" / "projects" / "alpha"
    home.mkdir()
    target.mkdir()
    artifact.mkdir(parents=True)
    (home / "workspace" / ".active_project").write_text("alpha\n")
    (artifact / "PROJECT.md").write_text(
        "---\nid: alpha\nname: alpha\nstatus: active\nproject_type: referenced\n"
        f"target_root: {target}\n---\n"
    )
    for name in ("ANCHOR_HOME", "ANCHOR_PROJECT_ID", "ANCHOR_PROJECT_ROOT"):
        monkeypatch.delenv(name, raising=False)

    anchor = launch(anchor_home=home, project_id="alpha", project_root=target)

    assert callable(anchor)
    status = anchor("status", output_format="dict")
    assert status["runtime"]["route_binding"]["project_id"] == "alpha"


def test_launch_derives_unique_project_from_verified_target(tmp_path, monkeypatch):
    home = tmp_path / "home"
    target = tmp_path / "target"
    artifact = home / "workspace" / "projects" / "alpha"
    target.mkdir()
    artifact.mkdir(parents=True)
    (artifact / "PROJECT.md").write_text(
        "---\nid: alpha\nname: alpha\nstatus: active\nproject_type: referenced\n"
        f"target_root: {target}\n---\n"
    )
    for name in ("ANCHOR_HOME", "ANCHOR_PROJECT_ID", "ANCHOR_PROJECT_ROOT"):
        monkeypatch.delenv(name, raising=False)

    anchor = launch(anchor_home=home, project_root=target)

    assert callable(anchor)
    status = anchor("status", output_format="dict")
    assert status["runtime"]["route_binding"]["project_id"] == "alpha"


def test_launch_passes_auto_configured_repository_provider(tmp_path, monkeypatch):
    import odibi_anchor.bootstrap as bootstrap_module

    home = tmp_path / "home"
    target = tmp_path / "target"
    artifact = home / "workspace" / "projects" / "alpha"
    target.mkdir()
    artifact.mkdir(parents=True)
    (artifact / "PROJECT.md").write_text(
        "---\nid: alpha\nname: alpha\nstatus: active\nproject_type: referenced\n"
        f"target_root: {target}\n---\n"
    )
    provider = object()
    observed = {}
    for name in ("ANCHOR_HOME", "ANCHOR_PROJECT_ID", "ANCHOR_PROJECT_ROOT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(
        startup_module,
        "_repository_provider_for_target",
        lambda candidate: provider if candidate == target else None,
    )

    def fake_init(**kwargs):
        observed.update(kwargs)
        return lambda *_args, **_kwargs: {}, str(target), {}

    monkeypatch.setattr(bootstrap_module, "init", fake_init)

    anchor = launch(anchor_home=home, project_id="alpha", project_root=target)

    assert callable(anchor)
    assert observed["repository_provider"] is provider
    assert observed["route_binding"].project_id == "alpha"


def test_local_target_does_not_attempt_databricks_repository_discovery(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "odibi_anchor.operational._databricks.autoconfigure_databricks_git_folder_repository",
        lambda *_args: pytest.fail("Databricks discovery must not run for a local target"),
    )

    assert startup_module._repository_provider_for_target(tmp_path) is None


def test_workspace_target_uses_databricks_repository_discovery(monkeypatch):
    target = Path("/Workspace/Users/test@example.invalid/project")
    provider = object()
    observed = []

    def autoconfigure(candidate):
        observed.append(candidate)
        return provider, {"acquisition_outcome": "available"}

    monkeypatch.setattr(
        "odibi_anchor.operational._databricks.autoconfigure_databricks_git_folder_repository",
        autoconfigure,
    )

    assert startup_module._repository_provider_for_target(target) is provider
    assert observed == [target]


@pytest.mark.parametrize("with_durability", [False, True])
def test_launch_refuses_database_owned_by_another_authority(
    tmp_path, monkeypatch, with_durability
):
    from odibi_anchor.durability import ensure_database_authority

    home = tmp_path / "home"
    target = tmp_path / "target"
    artifact = home / "workspace" / "projects" / "alpha"
    target.mkdir()
    artifact.mkdir(parents=True)
    (artifact / "PROJECT.md").write_text(
        "---\nid: alpha\nname: alpha\nstatus: active\nproject_type: referenced\n"
        f"target_root: {target}\n---\n"
    )
    ensure_database_authority(
        home / ".agent_memory.db", authority_id="authority-a",
        trust_domain="work", initialize=True,
    )
    monkeypatch.setenv("ANCHOR_HOME", str(home))
    monkeypatch.setenv("ANCHOR_MEMORY_DB", str(home / ".agent_memory.db"))
    monkeypatch.setenv("ANCHOR_AUTHORITY_ID", "authority-b")
    monkeypatch.setenv("ANCHOR_TRUST_DOMAIN", "work")
    if with_durability:
        durable = tmp_path / "durable"
        durable.mkdir()
        monkeypatch.setenv("ANCHOR_DURABLE_ROOT", str(durable))
    else:
        monkeypatch.delenv("ANCHOR_DURABLE_ROOT", raising=False)

    with pytest.raises(RuntimeError, match="authority identity conflicts"):
        launch(anchor_home=home, project_id="alpha", project_root=target)


def test_launch_stops_before_database_initialization_when_durable_root_is_unavailable(
    tmp_path, monkeypatch
):
    home = tmp_path / "home"
    target = tmp_path / "target"
    artifact = home / "workspace" / "projects" / "alpha"
    target.mkdir()
    artifact.mkdir(parents=True)
    (artifact / "PROJECT.md").write_text(
        "---\nid: alpha\nname: alpha\nstatus: active\nproject_type: referenced\n"
        f"target_root: {target}\n---\n"
    )
    database = home / ".agent_memory.db"
    monkeypatch.setenv("ANCHOR_HOME", str(home))
    monkeypatch.setenv("ANCHOR_MEMORY_DB", str(database))
    monkeypatch.setenv("ANCHOR_AUTHORITY_ID", "work")
    monkeypatch.setenv("ANCHOR_TRUST_DOMAIN", "work")
    monkeypatch.setenv("ANCHOR_DURABLE_ROOT", str(tmp_path / "missing-durable"))

    with pytest.raises(FileNotFoundError, match="durable_root is unavailable"):
        launch(anchor_home=home, project_id="alpha", project_root=target)
    assert not database.exists()


def test_register_project_prepares_exact_first_launch(tmp_path, monkeypatch):
    home = tmp_path / "new-home"
    target = tmp_path / "target"
    target.mkdir()
    for name in ("ANCHOR_HOME", "ANCHOR_PROJECT_ID", "ANCHOR_PROJECT_ROOT"):
        monkeypatch.delenv(name, raising=False)

    result = register_project(anchor_home=home, project_id="alpha", project_root=target)

    assert result["next_operation"] == {
        "operation": "launch",
        "arguments": {
            "anchor_home": str(home),
            "project_id": "alpha",
            "project_root": str(target),
        },
    }
    anchor = launch(**result["next_operation"]["arguments"])
    assert anchor("status", output_format="dict")["runtime"]["route_binding"]["project_id"] == "alpha"
    with pytest.raises(FileExistsError):
        register_project(anchor_home=home, project_id="alpha", project_root=target)


def test_prepare_portfolio_runtime_registers_exact_route_without_mutating_environment(
    tmp_path, monkeypatch
):
    config = tmp_path / "anchor.toml"
    target = tmp_path / "target"
    state = tmp_path / "state"
    target.mkdir()
    write_portfolio(
        config,
        {
            "schema_version": 1,
            "authority": {"id": "work", "trust_domain": "work"},
            "hosts": {"local": {"adapter": "amp", "local_state_root": str(state)}},
            "projects": {"alpha": {"targets": {"local": str(target)}}},
            "personas": {},
        },
    )
    before = dict(os.environ)

    result = prepare_portfolio_runtime(config_path=config, host_id="local", project_id="alpha")

    assert result["status"] == "ready"
    assert result["registration"]["status"] == "created"
    assert result["environment"]["ANCHOR_PROJECT_ID"] == "alpha"
    assert result["restore"]["status"] == "not_applicable"
    assert dict(os.environ) == before
    again = prepare_portfolio_runtime(config_path=config, host_id="local", project_id="alpha")
    assert again["registration"]["status"] == "existing"


def test_prepare_portfolio_runtime_stops_when_durable_storage_is_unavailable(
    tmp_path, monkeypatch
):
    config = tmp_path / "anchor.toml"
    target = tmp_path / "target"
    state = tmp_path / "state"
    unavailable = tmp_path / "unavailable-durable-root"
    target.mkdir()
    write_portfolio(
        config,
        {
            "schema_version": 1,
            "authority": {"id": "work", "trust_domain": "work"},
            "hosts": {"local": {
                "adapter": "amp", "local_state_root": str(state),
                "durable_root": str(unavailable),
            }},
            "projects": {"alpha": {"targets": {"local": str(target)}}},
            "personas": {},
        },
    )

    with pytest.raises(FileNotFoundError, match="durable_root is unavailable"):
        prepare_portfolio_runtime(config_path=config, host_id="local", project_id="alpha")
    assert not state.exists()


def test_prepare_databricks_runtime_and_launch_use_sdk_without_volume_fuse(
    tmp_path, monkeypatch
):
    from odibi_anchor import durability

    config = tmp_path / "anchor.toml"
    target = tmp_path / "target"
    state = tmp_path / "state"
    durable = "/Volumes/catalog/schema/anchor"
    target.mkdir()
    write_portfolio(
        config,
        {
            "schema_version": 1,
            "authority": {"id": "work", "trust_domain": "work"},
            "hosts": {"serverless": {
                "adapter": "databricks", "local_state_root": str(state),
                "durable_root": durable,
            }},
            "projects": {"alpha": {"targets": {"serverless": str(target)}}},
            "personas": {},
        },
    )
    calls = []
    monkeypatch.setattr(durability, "qualify_durability", lambda **kwargs: calls.append(("qualify", kwargs)))
    monkeypatch.setattr(
        durability,
        "list_snapshots",
        lambda **kwargs: calls.append(("list", kwargs)) or {"snapshots": [{"snapshot_id": "one"}]},
    )

    def restore(**kwargs):
        calls.append(("restore", kwargs))
        durability.ensure_database_authority(
            kwargs["destination_db"], authority_id="work", trust_domain="work", initialize=True,
        )
        return {"status": "restored", "snapshot_id": "one"}

    monkeypatch.setattr(durability, "restore_latest", restore)
    original_is_dir = Path.is_dir
    original_iterdir = Path.iterdir

    def reject_volume_is_dir(path):
        if str(path).startswith("/Volumes"):
            raise AssertionError(f"FUSE access attempted: {path}")
        return original_is_dir(path)

    def reject_volume_iterdir(path):
        if str(path).startswith("/Volumes"):
            raise AssertionError(f"FUSE access attempted: {path}")
        return original_iterdir(path)

    monkeypatch.setattr(Path, "is_dir", reject_volume_is_dir)
    monkeypatch.setattr(Path, "iterdir", reject_volume_iterdir)

    result = prepare_portfolio_runtime(
        config_path=config, host_id="serverless", project_id="alpha"
    )
    for name, value in result["environment"].items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("DATABRICKS_RUNTIME_VERSION", "serverless")

    anchor = launch(anchor_home=state, project_id="alpha", project_root=target)

    assert result["restore"] == {"status": "restored", "snapshot_id": "one"}
    assert callable(anchor)
    assert [name for name, _ in calls] == ["qualify", "list", "restore"]
    assert all(arguments["databricks"] is True for _, arguments in calls)


@pytest.mark.parametrize("error", [FileNotFoundError("absent"), PermissionError("denied")])
def test_prepare_databricks_runtime_only_treats_missing_snapshot_root_as_clean_start(
    tmp_path, monkeypatch, error
):
    from odibi_anchor import durability

    config = tmp_path / "anchor.toml"
    target = tmp_path / "target"
    state = tmp_path / "state"
    target.mkdir()
    write_portfolio(
        config,
        {
            "schema_version": 1,
            "authority": {"id": "work", "trust_domain": "work"},
            "hosts": {"serverless": {
                "adapter": "databricks", "local_state_root": str(state),
                "durable_root": "/Volumes/catalog/schema/anchor",
            }},
            "projects": {"alpha": {"targets": {"serverless": str(target)}}},
            "personas": {},
        },
    )
    monkeypatch.setattr(durability, "qualify_durability", lambda **_kwargs: None)
    monkeypatch.setattr(
        durability, "list_snapshots", lambda **_kwargs: (_ for _ in ()).throw(error)
    )

    if isinstance(error, PermissionError):
        with pytest.raises(PermissionError, match="denied"):
            prepare_portfolio_runtime(
                config_path=config, host_id="serverless", project_id="alpha"
            )
        assert not (state / ".agent_memory.db").exists()
    else:
        result = prepare_portfolio_runtime(
            config_path=config, host_id="serverless", project_id="alpha"
        )
        assert result["restore"] == {
            "status": "not_applicable", "reason": "no durable snapshot exists"
        }
        assert result["authority"]["status"] == "initialized"


def test_prepare_portfolio_runtime_refuses_existing_unowned_database(tmp_path):
    config = tmp_path / "anchor.toml"
    target = tmp_path / "target"
    state = tmp_path / "state"
    target.mkdir()
    state.mkdir()
    _legacy_db(state / ".agent_memory.db")
    write_portfolio(
        config,
        {
            "schema_version": 1,
            "authority": {"id": "work", "trust_domain": "work"},
            "hosts": {"local": {"adapter": "amp", "local_state_root": str(state)}},
            "projects": {"alpha": {"targets": {"local": str(target)}}},
            "personas": {},
        },
    )

    with pytest.raises(RuntimeError, match="no authority identity"):
        prepare_portfolio_runtime(config_path=config, host_id="local", project_id="alpha")


def test_doctor_is_read_only_secret_safe_and_truthful(tmp_path):
    home = tmp_path / "missing-home"
    target = tmp_path / "target"
    target.mkdir()
    before = set(tmp_path.rglob("*"))

    result = doctor(environment={"ANCHOR_HOME": str(home), "ANCHOR_PROJECT_ID": "alpha",
                                 "ANCHOR_PROJECT_ROOT": str(target), "TOKEN": "secret"})

    assert result["read_only"] is True
    assert result["route_inputs"] == {"ANCHOR_HOME": str(home), "ANCHOR_PROJECT_ID": "alpha",
                                      "ANCHOR_PROJECT_ROOT": str(target)}
    assert result["routing"]["status"] == "ambiguous_or_invalid"
    assert result["next_operation"]["operation"] == "register_project"
    assert result["tasks"]["status"] == "unavailable"
    assert result["concurrency"]["status"] == "unqualified"
    assert "secret" not in repr(result)
    assert set(tmp_path.rglob("*")) == before


def test_doctor_reports_copy_ready_databricks_dependency_remediation(tmp_path, monkeypatch):
    home = tmp_path / "home"
    target = tmp_path / "target"
    target.mkdir()
    monkeypatch.setattr(
        "odibi_anchor.startup.importlib.metadata.version",
        lambda _name: "0.137.0",
    )

    result = doctor(environment={
        "ANCHOR_HOME": str(home),
        "ANCHOR_PROJECT_ID": "alpha",
        "ANCHOR_PROJECT_ROOT": str(target),
        "DATABRICKS_RUNTIME_VERSION": "serverless",
    })

    capability = result["capabilities"]["databricks_sdk"]
    assert capability == {
        "status": "outdated",
        "required": True,
        "minimum_version": "0.138.0",
        "installed_version": "0.137.0",
        "qualified": False,
        "install_command": '%pip install "odibi-anchor[databricks]==0.3.1"',
        "restart_required_after_install": True,
    }
    assert result["next_operation"] == {
        "operation": "install_dependency",
        "command": '%pip install "odibi-anchor[databricks]==0.3.1"',
        "restart_python": True,
        "reason": "Databricks durability requires the qualified Workspace Files API SDK.",
    }


def test_doctor_accepts_semantically_equivalent_databricks_sdk_version(tmp_path, monkeypatch):
    target = tmp_path / "target"
    target.mkdir()
    monkeypatch.setattr(
        "odibi_anchor.startup.importlib.metadata.version",
        lambda _name: "0.138",
    )

    result = doctor(environment={
        "ANCHOR_HOME": str(tmp_path / "home"),
        "ANCHOR_PROJECT_ID": "alpha",
        "ANCHOR_PROJECT_ROOT": str(target),
        "DATABRICKS_RUNTIME_VERSION": "serverless",
    })

    assert result["capabilities"]["databricks_sdk"]["qualified"] is True
    assert result["capabilities"]["databricks_sdk"]["install_command"] is None
    assert result["next_operation"]["operation"] == "register_project"


def test_install_guidance_copies_packaged_contract_and_rejects_collision(tmp_path):
    target = tmp_path / "target"
    target.mkdir()

    result = install_guidance(target)

    assert result["status"] == "installed"
    assert result["file_count"] > 2
    assert len(result["content_sha256"]) == 64
    assert (target / ".assistant" / "agent_bootstrap.py").is_file()
    assert (target / ".assistant_instructions.md").is_file()
    with pytest.raises(RuntimeError, match="destination collision"):
        install_guidance(target)


def test_assistant_launcher_uses_installed_package_without_source_checkout(tmp_path):
    repository = Path(__file__).resolve().parents[1]
    home = tmp_path / "home"
    target = tmp_path / "target"
    project = home / "workspace" / "projects" / "alpha"
    launcher = target / ".assistant" / "agent_bootstrap.py"
    project.mkdir(parents=True)
    launcher.parent.mkdir(parents=True)
    shutil.copy2(repository / ".assistant" / "agent_bootstrap.py", launcher)
    (project / "PROJECT.md").write_text(
        "---\nid: alpha\nname: alpha\nstatus: active\nproject_type: referenced\n"
        f"target_root: {target}\n---\n"
    )
    script = (
        "import json,runpy,sys; n=runpy.run_path(sys.argv[1]); "
        "print('RESULT='+json.dumps(n['BOOTSTRAP'],sort_keys=True))"
    )
    environment = {
        **os.environ,
        "ANCHOR_HOME": str(home),
        "ANCHOR_PROJECT_ROOT": str(target),
        "PYTHONPATH": str(repository / "src"),
    }
    environment.pop("ANCHOR_PROJECT_ID", None)
    environment.pop("ANCHOR_SOURCE_CHECKOUT", None)

    completed = subprocess.run(
        [sys.executable, "-B", "-c", script, str(launcher)],
        env=environment, capture_output=True, text=True, check=True,
    )
    result = json.loads(next(
        line.removeprefix("RESULT=") for line in completed.stdout.splitlines()
        if line.startswith("RESULT=")
    ))
    assert result["runtime"] == "installed_distribution"
    assert result["project_id"] == "alpha"
    assert result["target_root"] == str(target)


def _legacy_db(path, *, overrides=None):
    from odibi_anchor.legacy_import import _LEGACY_V0110_SCHEMAS

    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE cw_schema_versions "
                       "(domain TEXT PRIMARY KEY,version INTEGER,schema_sha256 TEXT,applied_at TEXT)")
    values = dict(_LEGACY_V0110_SCHEMAS)
    values.update(overrides or {})
    connection.executemany(
        "INSERT INTO cw_schema_versions VALUES(?,?,?,?)",
        [(domain, version, checksum, "2026-09-10T00:00:00Z")
         for domain, (version, checksum) in values.items()],
    )
    connection.commit()
    connection.close()


def test_legacy_import_plans_without_mutation_then_backs_up_copies_and_launches(
    tmp_path, monkeypatch
):
    source = tmp_path / "cw"
    destination = tmp_path / "anchor"
    source.mkdir()
    _legacy_db(source / ".agent_memory.db")
    managed = source / "workspace" / "projects" / "managed-project"
    managed.mkdir(parents=True)
    (managed / "PROJECT.md").write_text(
        "---\nid: managed-project\nname: Managed\nstatus: active\n"
        f"project_type: \"managed\"\ntarget_root: \"{managed}\"\n---\n"
    )
    legacy_owner = managed / "continuity" / "v1" / "OWNER.json"
    legacy_owner.parent.mkdir(parents=True)
    legacy_owner.write_text("{}\n", encoding="utf-8")
    (source / "workspace" / "record.txt").write_text("legacy")
    source_digest = hashlib.sha256((source / ".agent_memory.db").read_bytes()).hexdigest()

    plan = plan_legacy_import(source, anchor_home=destination)
    assert not destination.exists()
    assert plan["live_history_imported"] is False
    result = apply_legacy_import(plan)

    assert result["status"] == "applied"
    assert result["history_disposition"] == "verified_backup_only"
    assert hashlib.sha256((source / ".agent_memory.db").read_bytes()).hexdigest() == source_digest
    assert (destination / "workspace" / "record.txt").read_text() == "legacy"
    assert not (destination / ".agent_memory.db").exists()
    backup = (destination / "legacy-import-backups" / plan["plan_id"].split(":", 1)[1]
              / ".agent_memory.db")
    assert backup.is_file()
    assert result["legacy_database_backup_sha256"] == hashlib.sha256(backup.read_bytes()).hexdigest()
    with sqlite3.connect(backup) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    imported = (destination / "workspace" / "projects" / "managed-project" / "PROJECT.md")
    assert f"target_root: {destination / 'workspace' / 'projects' / 'managed-project'}" in imported.read_text()
    assert (backup.parent / "workspace" / "projects" / "managed-project" /
            "continuity" / "v1" / "OWNER.json").is_file()
    assert not (imported.parent / "continuity").exists()

    monkeypatch.setenv("ANCHOR_HOME", str(destination))
    monkeypatch.setenv("ANCHOR_PROJECT_ID", "managed-project")
    monkeypatch.setenv("ANCHOR_PROJECT_ROOT", str(imported.parent))
    monkeypatch.setenv("ANCHOR_MEMORY_DB", str(destination / ".agent_memory.db"))
    anchor = launch(
        anchor_home=destination,
        project_id="managed-project",
        project_root=imported.parent,
    )
    assert anchor("status", output_format="dict")["runtime"]["route_binding"]["project_id"] == "managed-project"
    assert (imported.parent / "continuity" / "v1" / "OWNER.json").is_file()


def test_legacy_import_rejects_open_tasks_before_destination_mutation(tmp_path):
    source = tmp_path / "cw"
    destination = tmp_path / "anchor"
    source.mkdir()
    database = source / ".agent_memory.db"
    _legacy_db(database)
    (source / "workspace").mkdir()
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE accepted_task_records (task_window_id TEXT PRIMARY KEY)")
        connection.execute(
            "CREATE TABLE accepted_task_events "
            "(task_window_id TEXT,event_type TEXT)"
        )
        connection.execute("INSERT INTO accepted_task_records VALUES ('ltw_open')")

    with pytest.raises(RuntimeError, match="1 open task"):
        plan_legacy_import(source, anchor_home=destination)
    assert not destination.exists()


def test_legacy_import_fails_closed_on_collision_newer_schema_and_cw_env(tmp_path, monkeypatch):
    source = tmp_path / "selected"
    other = tmp_path / "implicit"
    destination = tmp_path / "anchor"
    source.mkdir()
    other.mkdir()
    destination.mkdir()
    (source / "workspace").mkdir()
    _legacy_db(source / ".agent_memory.db", overrides={"memory_lifecycle": (999, "f" * 64)})
    _legacy_db(other / ".agent_memory.db")
    monkeypatch.setenv("CW_HOME", str(other))

    with pytest.raises(RuntimeError, match="newer or incompatible"):
        plan_legacy_import(source, anchor_home=destination)

    (source / ".agent_memory.db").unlink()
    (destination / "workspace").mkdir()
    with pytest.raises(RuntimeError, match="destination collision"):
        plan_legacy_import(source, anchor_home=destination)


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ("project_type: referenced\n", "duplicate field"),
        ("", "unsupported project_type"),
    ],
)
def test_legacy_import_rejects_ambiguous_project_descriptors(tmp_path, extra, message):
    source = tmp_path / "cw"
    destination = tmp_path / "anchor"
    project = source / "workspace" / "projects" / "alpha"
    project.mkdir(parents=True)
    _legacy_db(source / ".agent_memory.db")
    kind = "unsupported" if not extra else "managed"
    (project / "PROJECT.md").write_text(
        f"---\nid: alpha\nproject_type: {kind}\n{extra}target_root: {project}\n---\n"
    )

    with pytest.raises(RuntimeError, match=message):
        plan_legacy_import(source, anchor_home=destination)
    assert not destination.exists()


def test_legacy_import_rejects_routing_quote_disagreement(tmp_path):
    source = tmp_path / "cw"
    project = source / "workspace" / "projects" / "alpha"
    project.mkdir(parents=True)
    _legacy_db(source / ".agent_memory.db")
    (project / "PROJECT.md").write_text(
        "---\nid: alpha\nproject_type: referenced\ntarget_root: \"/srv/project'\"\n---\n"
    )

    with pytest.raises(RuntimeError, match="ambiguous quoting"):
        plan_legacy_import(source, anchor_home=tmp_path / "anchor")


@pytest.mark.parametrize("entry", ["workspace", ".agent_memory.db"])
def test_legacy_import_rejects_symlinked_source_authority(tmp_path, entry):
    source = tmp_path / "cw"
    source.mkdir()
    real_workspace = tmp_path / "real-workspace"
    real_workspace.mkdir()
    real_database = tmp_path / "real.db"
    _legacy_db(real_database)
    if entry == "workspace":
        (source / "workspace").symlink_to(real_workspace, target_is_directory=True)
        shutil.copy2(real_database, source / ".agent_memory.db")
    else:
        (source / "workspace").mkdir()
        (source / ".agent_memory.db").symlink_to(real_database)

    with pytest.raises(RuntimeError, match="symlink"):
        plan_legacy_import(source, anchor_home=tmp_path / "anchor")


def test_legacy_import_rejects_dangling_destination_symlink(tmp_path):
    source = tmp_path / "cw"
    destination = tmp_path / "anchor"
    source.mkdir()
    (source / "workspace").mkdir()
    _legacy_db(source / ".agent_memory.db")
    destination.mkdir()
    (destination / "workspace").symlink_to(tmp_path / "missing", target_is_directory=True)

    with pytest.raises(RuntimeError, match="destination collision"):
        plan_legacy_import(source, anchor_home=destination)
    assert (destination / "workspace").is_symlink()


def test_legacy_import_rejects_symlinked_backup_destination(tmp_path):
    source = tmp_path / "cw"
    destination = tmp_path / "anchor"
    source.mkdir()
    (source / "workspace").mkdir()
    _legacy_db(source / ".agent_memory.db")
    plan = plan_legacy_import(source, anchor_home=destination)
    destination.mkdir()
    (destination / "legacy-import-backups").symlink_to(
        tmp_path / "outside", target_is_directory=True
    )

    with pytest.raises(RuntimeError, match="backup destination is a symlink"):
        apply_legacy_import(plan)
    assert not (tmp_path / "outside").exists()


def test_legacy_import_rollback_removes_only_entries_it_created(tmp_path, monkeypatch):
    source = tmp_path / "cw"
    destination = tmp_path / "anchor"
    source.mkdir()
    (source / "workspace").mkdir()
    (source / "workspace" / "record.txt").write_text("legacy")
    _legacy_db(source / ".agent_memory.db")
    plan = plan_legacy_import(source, anchor_home=destination)
    destination.mkdir()
    preserved = destination / "preserved.txt"
    preserved.write_text("keep")
    original_copytree = shutil.copytree

    def failing_copytree(source_path, target_path, *args, **kwargs):
        if Path(target_path) == destination / "workspace":
            (Path(target_path) / "partial.txt").write_text("partial")
            raise OSError("injected activation failure")
        return original_copytree(source_path, target_path, *args, **kwargs)

    monkeypatch.setattr("odibi_anchor.legacy_import.shutil.copytree", failing_copytree)
    with pytest.raises(OSError, match="injected activation failure"):
        apply_legacy_import(plan)

    assert preserved.read_text() == "keep"
    assert not (destination / "workspace").exists()


def test_legacy_import_does_not_delete_competing_destination(tmp_path, monkeypatch):
    source = tmp_path / "cw"
    destination = tmp_path / "anchor"
    source.mkdir()
    (source / "workspace").mkdir()
    _legacy_db(source / ".agent_memory.db")
    plan = plan_legacy_import(source, anchor_home=destination)
    target = destination / "workspace"
    original_mkdir = Path.mkdir
    injected = False

    def racing_mkdir(path, *args, **kwargs):
        nonlocal injected
        if path == target and not injected:
            injected = True
            original_mkdir(path)
            (path / "competing.txt").write_text("keep")
        return original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", racing_mkdir)
    with pytest.raises(FileExistsError):
        apply_legacy_import(plan)

    assert (target / "competing.txt").read_text() == "keep"


def test_legacy_import_detects_file_to_directory_snapshot_drift(tmp_path, monkeypatch):
    source = tmp_path / "cw"
    destination = tmp_path / "anchor"
    source.mkdir()
    workspace = source / "workspace"
    workspace.mkdir()
    changing = workspace / "changing"
    changing.write_text("")
    _legacy_db(source / ".agent_memory.db")
    plan = plan_legacy_import(source, anchor_home=destination)
    original_copytree = shutil.copytree
    injected = False

    def drifting_copytree(source_path, target_path, *args, **kwargs):
        nonlocal injected
        if Path(source_path) == workspace and not injected:
            injected = True
            changing.unlink()
            changing.mkdir()
        return original_copytree(source_path, target_path, *args, **kwargs)

    monkeypatch.setattr("odibi_anchor.legacy_import.shutil.copytree", drifting_copytree)
    with pytest.raises(RuntimeError, match="workspace changed"):
        apply_legacy_import(plan)

    assert not (destination / "workspace").exists()
