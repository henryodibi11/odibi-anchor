import os
import sqlite3

import pytest

from odibi_anchor.legacy_import import apply_legacy_import, plan_legacy_import
from odibi_anchor.startup import doctor, launch


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
    assert os.environ["ANCHOR_HOME"] == str(home.resolve())
    assert os.environ["ANCHOR_PROJECT_ID"] == "alpha"
    assert os.environ["ANCHOR_PROJECT_ROOT"] == str(target.resolve())


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


def _legacy_db(path, version=1, checksum=None):
    from odibi_anchor.codebase._memory_lifecycle import SCHEMA_SHA256

    checksum = SCHEMA_SHA256 if checksum is None else checksum
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE anchor_schema_versions "
                       "(domain TEXT PRIMARY KEY,version INTEGER,schema_sha256 TEXT)")
    connection.execute("INSERT INTO anchor_schema_versions VALUES(?,?,?)",
                       ("memory_lifecycle", version, checksum))
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


def test_legacy_import_fails_closed_on_collision_newer_schema_and_cw_env(tmp_path, monkeypatch):
    source = tmp_path / "selected"
    other = tmp_path / "implicit"
    destination = tmp_path / "anchor"
    source.mkdir()
    other.mkdir()
    destination.mkdir()
    _legacy_db(source / ".agent_memory.db", version=999)
    _legacy_db(other / ".agent_memory.db")
    monkeypatch.setenv("CW_HOME", str(other))

    with pytest.raises(RuntimeError, match="newer or incompatible"):
        plan_legacy_import(source, anchor_home=destination)

    (source / ".agent_memory.db").unlink()
    (source / "workspace").mkdir()
    (destination / "workspace").mkdir()
    with pytest.raises(RuntimeError, match="destination collision"):
        plan_legacy_import(source, anchor_home=destination)
