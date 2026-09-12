import hashlib
import io
import json
import os
import runpy
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import odibi_anchor.startup as startup_module
from odibi_anchor.legacy_import import apply_legacy_import, plan_legacy_import
from odibi_anchor.portfolio import write_portfolio
from odibi_anchor.startup import (
    bootstrap_managed_project,
    doctor,
    install_guidance,
    launch,
    prepare_portfolio_runtime,
    register_project,
)


def _managed_orientation(project_id: str, target: Path, artifact: Path) -> dict:
    return {
        "kind": "orientation",
        "status": {
            "runtime": {
                "route_binding": {
                    "project_id": project_id,
                    "target_root": str(target),
                    "artifact_root": str(artifact),
                    "binding_source": "explicit",
                }
            }
        },
        "metrics": {"next_required_action": "new_session"},
        "managed_artifact_actions": [
            {
                "artifact": "work_items/",
                "name": "work_item",
                "list_or_show": 'anchor("work_item", "list", output_format="dict")',
            }
        ],
    }


def test_bootstrap_managed_project_infers_host_and_returns_compact_packet(
    tmp_path, monkeypatch
):
    instruction = tmp_path / "instructions"
    target = tmp_path / "target"
    home = tmp_path / "state"
    artifact = home / "workspace" / "projects" / "alpha"
    config = tmp_path / "anchor.toml"
    instruction.mkdir()
    target.mkdir()
    write_portfolio(config, {
        "schema_version": 1,
        "authority": {"id": "owner", "trust_domain": "work"},
        "hosts": {"local": {
            "adapter": "amp", "local_state_root": str(home),
            "instruction_root": str(instruction),
        }},
        "projects": {"alpha": {"targets": {"local": str(target)}}},
        "personas": {},
    })
    prepared = {
        "environment": {
            "ANCHOR_HOME": str(home), "ANCHOR_MEMORY_DB": str(home / ".agent_memory.db"),
            "ANCHOR_AUTHORITY_ID": "owner", "ANCHOR_TRUST_DOMAIN": "work",
            "ANCHOR_PROJECT_ID": "alpha", "ANCHOR_PROJECT_ROOT": str(target),
        },
        "target_root": str(target), "restore": {"action": "not_applicable"},
    }
    observed = {}
    monkeypatch.setattr("odibi_anchor.host_setup.setup_host", lambda root, adapter: {
        "status": "unchanged", "verified_file_count": 92, "verified_skill_count": 18,
    })
    def fake_prepare(**kwargs):
        observed["prepare"] = kwargs
        return prepared

    monkeypatch.setattr(startup_module, "prepare_portfolio_runtime", fake_prepare)

    def fake_launch(**kwargs):
        observed["launch"] = kwargs
        return lambda action, **_kwargs: _managed_orientation("alpha", target, artifact)

    monkeypatch.setattr(startup_module, "launch", fake_launch)
    isolated_environment = dict(os.environ)
    for name in prepared["environment"]:
        isolated_environment.pop(name, None)
    monkeypatch.setattr(startup_module.os, "environ", isolated_environment)

    result = bootstrap_managed_project(
        config_path=config, project_id="alpha", instruction_root=instruction
    )

    assert observed["prepare"] == {
        "config_path": config, "host_id": "local", "project_id": "alpha",
    }
    assert observed["launch"]["project_root"] == str(target)
    assert result["startup_packet"] == {
        "kind": "managed_startup_packet", "status": "ready", "project_id": "alpha",
        "host_id": "local", "target_root": str(target), "artifact_root": str(artifact),
        "binding_source": "explicit",
        "guidance": {"status": "unchanged", "verified_files": 92, "verified_skills": 18},
        "restore": {"action": "not_applicable"}, "project_created": False,
        "portfolio_change": None, "durable_checkpoint": None,
        "managed_artifact_actions": result["orientation"]["managed_artifact_actions"],
        "memory_scope_semantics": {
            "project_local": "eligible only inside its exact managed project",
            "all": "eligible across projects when relevant; not selected for every task",
        },
        "next_required_action": "new_session",
    }


def test_bootstrap_managed_project_refuses_implicit_creation(tmp_path, monkeypatch):
    instruction = tmp_path / "instructions"
    target = tmp_path / "target"
    config = tmp_path / "anchor.toml"
    instruction.mkdir()
    target.mkdir()
    before = write_portfolio(config, {
        "schema_version": 1,
        "authority": {"id": "owner", "trust_domain": "work"},
        "hosts": {"local": {
            "adapter": "amp", "local_state_root": str(tmp_path / "state"),
            "instruction_root": str(instruction),
        }},
        "projects": {}, "personas": {},
    })["sha256"]
    monkeypatch.setattr("odibi_anchor.host_setup.setup_host", lambda *_args, **_kwargs: {})

    with pytest.raises(RuntimeError, match="creation requires explicit user authorization"):
        bootstrap_managed_project(
            config_path=config, project_id="new-project", instruction_root=instruction,
            project_root=target,
        )

    assert hashlib.sha256(config.read_bytes()).hexdigest() == before


def test_bootstrap_managed_project_refuses_stale_retention_environment(
    tmp_path, monkeypatch
):
    instruction = tmp_path / "instructions"
    target = tmp_path / "target"
    home = tmp_path / "state"
    config = tmp_path / "anchor.toml"
    instruction.mkdir()
    target.mkdir()
    write_portfolio(config, {
        "schema_version": 1,
        "authority": {"id": "owner", "trust_domain": "work"},
        "hosts": {"local": {
            "adapter": "amp", "local_state_root": str(home),
            "instruction_root": str(instruction),
        }},
        "projects": {"alpha": {"targets": {"local": str(target)}}},
        "personas": {},
    })
    monkeypatch.setattr("odibi_anchor.host_setup.setup_host", lambda *_args, **_kwargs: {})
    monkeypatch.setenv("ANCHOR_RETENTION_DAYS", "1")

    with pytest.raises(RuntimeError, match=r"restart Python.*ANCHOR_RETENTION_DAYS"):
        bootstrap_managed_project(
            config_path=config, project_id="alpha", instruction_root=instruction
        )


def test_bootstrap_managed_project_explicit_creation_checkpoints_state(
    tmp_path, monkeypatch
):
    instruction = tmp_path / "instructions"
    target = tmp_path / "target"
    home = tmp_path / "state"
    durable = tmp_path / "durable"
    artifact = home / "workspace" / "projects" / "new-project"
    config = tmp_path / "anchor.toml"
    instruction.mkdir()
    target.mkdir()
    durable.mkdir()
    write_portfolio(config, {
        "schema_version": 1,
        "authority": {"id": "owner", "trust_domain": "work"},
        "hosts": {"local": {
            "adapter": "amp", "local_state_root": str(home),
            "instruction_root": str(instruction), "durable_root": str(durable),
        }},
        "projects": {}, "personas": {},
    })
    prepared = {
        "environment": {
            "ANCHOR_HOME": str(home), "ANCHOR_MEMORY_DB": str(home / ".agent_memory.db"),
            "ANCHOR_AUTHORITY_ID": "owner", "ANCHOR_TRUST_DOMAIN": "work",
            "ANCHOR_PROJECT_ID": "new-project", "ANCHOR_PROJECT_ROOT": str(target),
            "ANCHOR_DURABLE_ROOT": str(durable),
        },
        "target_root": str(target), "restore": {"action": "created"},
    }
    monkeypatch.setattr("odibi_anchor.host_setup.setup_host", lambda *_args, **_kwargs: {
        "status": "installed", "verified_file_count": 1, "verified_skill_count": 1,
    })
    monkeypatch.setattr(startup_module, "prepare_portfolio_runtime", lambda **_kwargs: prepared)
    monkeypatch.setattr(startup_module, "launch", lambda **_kwargs: (
        lambda action, **kwargs: _managed_orientation("new-project", target, artifact)
    ))
    checkpoint_calls = []
    monkeypatch.setattr("odibi_anchor.durability.snapshot_state", lambda **kwargs: (
        checkpoint_calls.append(kwargs) or {"snapshot_id": "checkpoint"}
    ))
    isolated_environment = dict(os.environ)
    for name in prepared["environment"]:
        isolated_environment.pop(name, None)
    monkeypatch.setattr(startup_module.os, "environ", isolated_environment)

    result = bootstrap_managed_project(
        config_path=config, project_id="new-project", instruction_root=instruction,
        create_if_missing=True, project_root=target,
    )

    assert result["startup_packet"]["project_created"] is True
    assert result["startup_packet"]["durable_checkpoint"] == {"snapshot_id": "checkpoint"}
    assert checkpoint_calls[0]["source_artifacts"] == home / "workspace" / "projects"
    assert checkpoint_calls[0]["databricks"] is False
    from odibi_anchor.portfolio import load_portfolio
    assert load_portfolio(config)["projects"]["new-project"]["targets"]["local"] == str(target)


def test_bootstrap_managed_project_conflict_precedes_creation_write(tmp_path, monkeypatch):
    instruction = tmp_path / "instructions"
    target = tmp_path / "target"
    config = tmp_path / "anchor.toml"
    instruction.mkdir()
    target.mkdir()
    before = write_portfolio(config, {
        "schema_version": 1,
        "authority": {"id": "owner", "trust_domain": "work"},
        "hosts": {"local": {
            "adapter": "amp", "local_state_root": str(tmp_path / "state"),
            "instruction_root": str(instruction),
        }},
        "projects": {}, "personas": {},
    })["sha256"]
    monkeypatch.setattr("odibi_anchor.host_setup.setup_host", lambda *_args, **_kwargs: {})
    monkeypatch.setenv("ANCHOR_PROJECT_ID", "other-project")

    with pytest.raises(RuntimeError, match="restart Python before binding another project"):
        bootstrap_managed_project(
            config_path=config, project_id="new-project", instruction_root=instruction,
            create_if_missing=True, project_root=target,
        )

    assert hashlib.sha256(config.read_bytes()).hexdigest() == before


def test_bootstrap_managed_project_refuses_ambiguous_instruction_host(tmp_path):
    instruction = tmp_path / "instructions"
    target = tmp_path / "target"
    config = tmp_path / "anchor.toml"
    instruction.mkdir()
    target.mkdir()
    write_portfolio(config, {
        "schema_version": 1,
        "authority": {"id": "owner", "trust_domain": "work"},
        "hosts": {
            "one": {
                "adapter": "amp", "local_state_root": str(tmp_path / "one"),
                "instruction_root": str(instruction),
            },
            "two": {
                "adapter": "amp", "local_state_root": str(tmp_path / "two"),
                "instruction_root": str(instruction),
            },
        },
        "projects": {"alpha": {"targets": {
            "one": str(target), "two": str(target),
        }}},
        "personas": {},
    })

    with pytest.raises(ValueError, match="multiple portfolio hosts"):
        bootstrap_managed_project(
            config_path=config, project_id="alpha", instruction_root=instruction,
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


def test_prepare_portfolio_runtime_restores_v2_database_and_managed_projects(tmp_path):
    from odibi_anchor import durability

    config = tmp_path / "anchor.toml"
    target = tmp_path / "target"
    state = tmp_path / "state"
    durable = tmp_path / "durable"
    target.mkdir()
    durable.mkdir()
    register_project(anchor_home=state, project_id="alpha", project_root=target)
    database = state / ".agent_memory.db"
    projects = state / "workspace" / "projects"
    (projects / "alpha" / "problems" / "P-1.md").write_text("# Recovered\n")
    durability.ensure_database_authority(
        database, authority_id="work", trust_domain="work", initialize=True
    )
    snapshot = durability.snapshot_state(
        source_db=database,
        source_artifacts=projects,
        durable_root=durable,
        authority_id="work",
    )
    shutil.rmtree(state)
    write_portfolio(
        config,
        {
            "schema_version": 1,
            "authority": {"id": "work", "trust_domain": "work"},
            "hosts": {"local": {
                "adapter": "amp", "local_state_root": str(state),
                "durable_root": str(durable),
            }},
            "projects": {"alpha": {"targets": {"local": str(target)}}},
            "personas": {},
        },
    )

    result = prepare_portfolio_runtime(config_path=config, host_id="local", project_id="alpha")

    assert result["restore"]["snapshot_id"] == snapshot["manifest"]["snapshot_id"]
    assert result["restore"]["artifacts"]["status"] == "restored"
    assert result["registration"]["status"] == "existing"
    assert (projects / "alpha" / "problems" / "P-1.md").read_text() == "# Recovered\n"


def test_prepare_portfolio_runtime_relocates_continuity_to_new_local_state_root(
    tmp_path, monkeypatch
):
    from odibi_anchor import durability
    from odibi_anchor._dispatcher._project import resolve_route_binding
    from odibi_anchor._dispatcher._session import _save_continuity_state

    config = tmp_path / "anchor.toml"
    target = tmp_path / "target"
    old_state = tmp_path / "old-state"
    new_state = tmp_path / "new-state"
    durable = tmp_path / "durable"
    target.mkdir()
    durable.mkdir()
    register_project(anchor_home=old_state, project_id="alpha", project_root=target)
    database = old_state / ".agent_memory.db"
    durability.ensure_database_authority(
        database, authority_id="work", trust_domain="work", initialize=True
    )
    route = resolve_route_binding(
        old_state,
        project="alpha",
        target_hint=target,
        runtime_instance_id="runtime-a",
    )
    assert route is not None
    _save_continuity_state(
        route,
        SimpleNamespace(
            session_id="session-a",
            task_window_id="task-a",
            continuity_generation=0,
            continuity_record_sha256=None,
            continuity_status="uninitialized",
        ),
        {"open_task": "task-a"},
    )
    durability.snapshot_state(
        source_db=database,
        source_artifacts=old_state / "workspace" / "projects",
        durable_root=durable,
        authority_id="work",
    )
    write_portfolio(
        config,
        {
            "schema_version": 1,
            "authority": {"id": "work", "trust_domain": "work"},
            "hosts": {"local": {
                "adapter": "amp", "local_state_root": str(new_state),
                "durable_root": str(durable),
            }},
            "projects": {"alpha": {"targets": {"local": str(target)}}},
            "personas": {},
        },
    )

    result = prepare_portfolio_runtime(config_path=config, host_id="local", project_id="alpha")
    for name, value in result["environment"].items():
        monkeypatch.setenv(name, value)
    anchor = launch(anchor_home=new_state, project_id="alpha", project_root=target)

    assert result["restore"]["artifacts"]["continuity"] == {
        "status": "relocated",
        "owners_relocated": 1,
        "records_relocated": 1,
    }
    assert callable(anchor)
    status = anchor("status", output_format="dict")
    assert status["runtime"]["route_binding"]["project_id"] == "alpha"


@pytest.mark.parametrize("remove", ["database", "projects"])
def test_prepare_portfolio_runtime_refuses_partial_local_v2_state(tmp_path, remove):
    from odibi_anchor import durability

    config = tmp_path / "anchor.toml"
    target = tmp_path / "target"
    state = tmp_path / "state"
    durable = tmp_path / "durable"
    target.mkdir()
    durable.mkdir()
    register_project(anchor_home=state, project_id="alpha", project_root=target)
    database = state / ".agent_memory.db"
    projects = state / "workspace" / "projects"
    durability.ensure_database_authority(
        database, authority_id="work", trust_domain="work", initialize=True
    )
    durability.snapshot_state(
        source_db=database,
        source_artifacts=projects,
        durable_root=durable,
        authority_id="work",
    )
    if remove == "database":
        database.unlink()
    else:
        shutil.rmtree(projects)
    write_portfolio(
        config,
        {
            "schema_version": 1,
            "authority": {"id": "work", "trust_domain": "work"},
            "hosts": {"local": {
                "adapter": "amp", "local_state_root": str(state),
                "durable_root": str(durable),
            }},
            "projects": {"alpha": {"targets": {"local": str(target)}}},
            "personas": {},
        },
    )

    with pytest.raises(RuntimeError, match="local durable state is partial"):
        prepare_portfolio_runtime(config_path=config, host_id="local", project_id="alpha")


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


def test_doctor_directs_unconfigured_databricks_to_portfolio_prepare(monkeypatch):
    monkeypatch.setattr(
        "odibi_anchor.startup.importlib.metadata.version",
        lambda _name: "0.138.0",
    )

    result = doctor(environment={
        "DATABRICKS_RUNTIME_VERSION": "serverless",
        "TOKEN": "secret",
    })

    assert result["read_only"] is True
    assert result["home"] == {"path": None, "exists": False, "status": "unconfigured"}
    assert result["database"] == {
        "path": None, "exists": False, "status": "unconfigured"
    }
    assert result["routing"]["status"] == "unconfigured"
    assert result["next_operation"] == {
        "operation": "portfolio.prepare",
        "required_inputs": ["config_path", "host_id", "project_id"],
        "command": (
            "anchor portfolio prepare --config <absolute-config> "
            "--host <host-id> --project <project-id>"
        ),
        "reason": (
            "Portfolio preparation selects local live state, durable snapshots, "
            "and the exact managed-project route. Do not set ANCHOR_HOME manually."
        ),
    }
    assert result["tasks"]["status"] == "unavailable"
    assert "secret" not in repr(result)


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
        "install_command": '%pip install "odibi-anchor[databricks]==0.3.10"',
        "restart_required_after_install": True,
    }
    assert result["next_operation"] == {
        "operation": "install_dependency",
        "command": '%pip install "odibi-anchor[databricks]==0.3.10"',
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


def test_assistant_launcher_resolves_exact_latest_stable_databricks_install(
    tmp_path, monkeypatch
):
    repository = Path(__file__).resolve().parents[1]
    host = tmp_path / "host"
    launcher = host / ".assistant" / "agent_bootstrap.py"
    launcher.parent.mkdir(parents=True)
    shutil.copy2(repository / ".assistant" / "agent_bootstrap.py", launcher)
    payload = {
        "releases": {
            "0.3.10": [{"yanked": False}],
            "0.4.0rc1": [{"yanked": False}],
            "9.9.9": [{"yanked": True}],
        }
    }
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *_args, **_kwargs: io.BytesIO(json.dumps(payload).encode()),
    )
    monkeypatch.setattr("importlib.metadata.version", lambda _name: "0.3.6")
    monkeypatch.setenv("DATABRICKS_RUNTIME_VERSION", "serverless")
    monkeypatch.delenv("ANCHOR_SOURCE_CHECKOUT", raising=False)

    with pytest.raises(RuntimeError) as raised:
        runpy.run_path(str(launcher))

    message = str(raised.value)
    assert '%pip install "odibi-anchor[databricks]==0.3.10"' in message
    assert "dbutils.library.restartPython()" in message
    assert "0.4.0rc1" not in message
    assert "9.9.9" not in message


def test_assistant_launcher_bootstraps_managed_project_from_id_only(
    tmp_path, monkeypatch, capsys
):
    import odibi_anchor

    repository = Path(__file__).resolve().parents[1]
    host = tmp_path / "host"
    target = tmp_path / "target"
    launcher = host / ".assistant" / "agent_bootstrap.py"
    config = host / ".odibi-anchor" / "anchor.toml"
    launcher.parent.mkdir(parents=True)
    config.parent.mkdir(parents=True)
    target.mkdir()
    shutil.copy2(repository / ".assistant" / "agent_bootstrap.py", launcher)
    config.write_text("managed by test\n", encoding="utf-8")
    packet = {
        "kind": "managed_startup_packet", "status": "ready", "project_id": "alpha",
        "target_root": str(target), "managed_artifact_actions": [],
        "next_required_action": "new_session",
    }
    observed = {}

    def fake_bootstrap_managed_project(**kwargs):
        observed.update(kwargs)
        return {
            "anchor": lambda *_args, **_kwargs: {}, "root": str(target), "manifest": None,
            "orientation": {"kind": "orientation"}, "startup_packet": packet,
            "preparation": {"environment": {"ANCHOR_HOME": str(tmp_path / "state")}},
        }

    monkeypatch.setattr(
        odibi_anchor, "bootstrap_managed_project", fake_bootstrap_managed_project
    )
    monkeypatch.delenv("DATABRICKS_RUNTIME_VERSION", raising=False)
    monkeypatch.delenv("ANCHOR_SOURCE_CHECKOUT", raising=False)
    monkeypatch.delenv("ANCHOR_PROJECT_ID", raising=False)

    namespace = runpy.run_path(
        str(launcher), init_globals={"ANCHOR_PROJECT_ID": "alpha"}
    )

    assert observed == {
        "config_path": config, "project_id": "alpha", "instruction_root": host,
        "create_if_missing": False, "project_root": None,
    }
    assert namespace["STARTUP_PACKET"] == packet
    assert namespace["BOOTSTRAP"]["runtime"] == "managed_installed_distribution"
    printed = capsys.readouterr().out
    assert 'ODIBI_ANCHOR_STARTUP={"kind": "managed_startup_packet"' in printed


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
