import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from odibi_anchor.legacy_import import apply_legacy_import, plan_legacy_import
from odibi_anchor.startup import doctor, install_guidance, launch


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
    assert result["tasks"]["status"] == "unavailable"
    assert result["concurrency"]["status"] == "unqualified"
    assert "secret" not in repr(result)
    assert set(tmp_path.rglob("*")) == before


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


def test_legacy_import_plans_without_mutation_then_backs_up_and_copies(tmp_path):
    source = tmp_path / "cw"
    destination = tmp_path / "anchor"
    source.mkdir()
    _legacy_db(source / ".agent_memory.db")
    (source / "workspace").mkdir()
    (source / "workspace" / "record.txt").write_text("legacy")

    plan = plan_legacy_import(source, anchor_home=destination)
    assert not destination.exists()
    result = apply_legacy_import(plan)

    assert result["status"] == "applied"
    assert (destination / "workspace" / "record.txt").read_text() == "legacy"
    assert (destination / ".agent_memory.db").is_file()
    assert (destination / "legacy-import-backups" / plan["plan_id"].split(":", 1)[1]
            / ".agent_memory.db").is_file()
    with sqlite3.connect(destination / ".agent_memory.db") as connection:
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        assert "anchor_schema_versions" in tables
        assert "cw_schema_versions" not in tables
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_legacy_import_fails_closed_on_collision_newer_schema_and_cw_env(tmp_path, monkeypatch):
    source = tmp_path / "selected"
    other = tmp_path / "implicit"
    destination = tmp_path / "anchor"
    source.mkdir()
    other.mkdir()
    destination.mkdir()
    _legacy_db(source / ".agent_memory.db", overrides={"memory_lifecycle": (999, "f" * 64)})
    _legacy_db(other / ".agent_memory.db")
    monkeypatch.setenv("CW_HOME", str(other))

    with pytest.raises(RuntimeError, match="newer or incompatible"):
        plan_legacy_import(source, anchor_home=destination)

    (source / ".agent_memory.db").unlink()
    (source / "workspace").mkdir()
    (destination / "workspace").mkdir()
    with pytest.raises(RuntimeError, match="destination collision"):
        plan_legacy_import(source, anchor_home=destination)
