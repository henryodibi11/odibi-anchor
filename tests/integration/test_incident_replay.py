"""End-to-end replays of real Odibi Anchor incidents on a fake Databricks runtime.

Each scenario reproduces one field incident through public entry points and
asserts the shared v0.3.24 error-code contract (``error_code`` plus key context),
never message text. Scenarios whose fix is still in flight are strict xfails that
name the owning workstream; flip them when the fix merges.
"""
from __future__ import annotations

import errno
import importlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.fixtures.fake_databricks import DEFAULT_VOLUME
from tests.fixtures.fake_databricks.crash import CrashInjector

PLAIN_MARKDOWN_DESCRIPTOR = "# Alpha\n\nTeam notes about this project.\n"


def _closed_task_snapshot(anchor: Any, replay: Any) -> dict[str, Any]:
    """Accept and close one inquiry task; closure publishes a durable snapshot."""
    replay.start_analysis_task(anchor)
    closure = anchor("learning", "assess", outcome="nothing_reusable_learned", output_format="dict")
    assert closure["accepted_task_closure"]["status"] == "closed"
    assert closure["durable_state"]["transport"] == "databricks_files_api"
    return closure["durable_state"]


def _recovery(exc: BaseException) -> tuple[str | None, dict[str, Any]]:
    return getattr(exc, "error_code", None), dict(getattr(exc, "context", {}) or {})


# ── fresh compute (passes today) ─────────────────────────────────────────────


def test_fresh_compute_restores_route_and_continuity(replay, databricks):
    target = replay.target("alpha")
    created = replay.create_project("alpha", target)
    first = created["startup_packet"]
    assert first["project_created"] is True
    assert first["durable_checkpoint"]["action"] == "created"
    assert first["local_state"]["selection"] == "identity_isolated"
    snapshot = _closed_task_snapshot(created["anchor"], replay)
    assert snapshot["manifest_path"].startswith(f"{DEFAULT_VOLUME}/anchor/henry/snapshots/")

    replay.new_compute()
    assert databricks.runtime_roots() == []
    restored = replay.bootstrap("alpha")

    packet = restored["startup_packet"]
    runtime_root = Path(packet["local_state"]["runtime_root"])
    assert runtime_root != Path(first["local_state"]["runtime_root"])
    assert packet["local_state"]["compute_identity"] != first["local_state"]["compute_identity"]
    assert packet["restore"]["action"] == "created"
    assert packet["restore"]["snapshot_id"] == snapshot["manifest"]["snapshot_id"]
    restore = restored["preparation"]["restore"]
    assert restore["logical_digest"] == snapshot["manifest"]["logical_digest"]
    assert (packet["project_id"], packet["target_root"]) == ("alpha", str(target))
    artifact_root = runtime_root / "workspace" / "projects" / "alpha"
    assert packet["artifact_root"] == str(artifact_root)
    continuity = restore["artifacts"]["continuity"]
    assert continuity["status"] == "relocated"
    assert continuity["destination_home"] == str(runtime_root)
    owner = json.loads((artifact_root / "continuity" / "v1" / "OWNER.json").read_text())
    assert owner == {
        "anchor_home": str(runtime_root), "artifact_root": str(artifact_root),
        "project_id": "alpha", "schema_version": 1, "target_root": str(target),
    }
    # The restored route accepts fresh task authority and checkpoints again.
    follow_up = _closed_task_snapshot(restored["anchor"], replay)
    assert follow_up["manifest"]["snapshot_id"] != snapshot["manifest"]["snapshot_id"]


# ── #29 descriptor loss ──────────────────────────────────────────────────────


@pytest.mark.xfail(strict=True, reason="#29 WS-A pending: managed_descriptor_damaged")
@pytest.mark.parametrize("resume", ["restart", "new_compute"])
def test_plain_markdown_descriptor_fails_closed_as_damaged(replay, resume):
    created = replay.create_project("alpha")
    artifact_root = Path(created["startup_packet"]["artifact_root"])
    descriptor = artifact_root / "PROJECT.md"
    descriptor.write_text(PLAIN_MARKDOWN_DESCRIPTOR, encoding="utf-8")
    if resume == "new_compute":
        # The live dispatcher blocks once routing changes; an operator snapshot still
        # publishes the damaged descriptor durably.
        replay.snapshot(created)
        replay.new_compute()
    else:
        replay.new_process()

    with pytest.raises(Exception) as caught:
        replay.bootstrap("alpha")

    code, context = _recovery(caught.value)
    assert code == "managed_descriptor_damaged"
    damaged_root = Path(context["artifact_root"])
    assert context["project_id"] == "alpha"
    assert context["integrity_status"] == "missing_frontmatter"
    assert Path(context["descriptor_path"]) == damaged_root / "PROJECT.md"
    assert (damaged_root / "PROJECT.md").read_text(encoding="utf-8") == PLAIN_MARKDOWN_DESCRIPTOR


# ── #30 target moves ─────────────────────────────────────────────────────────


@pytest.mark.xfail(strict=True, reason="#30 WS-A pending: project_retarget_requires_migration")
def test_set_target_on_launched_project_is_refused_and_project_still_boots(replay, workflow):
    original = replay.target("alpha")
    moved = replay.target("alpha-moved")
    created = replay.create_project("alpha", original)
    anchor = created["anchor"]
    _closed_task_snapshot(anchor, replay)

    def call(action, *args, **kwargs):
        return anchor(action, *args, output_format="dict", **kwargs)

    workflow.accept(call, workflow.plan("artifact_only", ["PROJECT.md"]), name="retarget")
    descriptor = Path(created["startup_packet"]["artifact_root"]) / "PROJECT.md"
    before = descriptor.read_bytes()

    with pytest.raises(Exception) as caught:
        call("project", "set_target", "alpha", target=str(moved))

    assert _recovery(caught.value)[0] == "project_retarget_requires_migration"
    assert descriptor.read_bytes() == before
    replay.new_process()
    rebooted = replay.bootstrap("alpha")
    assert rebooted["startup_packet"]["target_root"] == str(original)


@pytest.mark.xfail(strict=True, reason="#30 WS-A pending: route_target_conflict probable_move")
def test_portfolio_target_move_on_fresh_compute_is_classified(replay):
    original = replay.target("alpha")
    created = replay.create_project("alpha", original)
    _closed_task_snapshot(created["anchor"], replay)
    moved = original.with_name("alpha-moved")
    original.rename(moved)
    document = importlib.import_module("odibi_anchor.portfolio").load_portfolio_document(replay.config)
    replay.write_portfolio({"alpha": str(moved)}, expected_sha256=document["sha256"])
    replay.new_compute()

    with pytest.raises(Exception) as caught:
        replay.bootstrap("alpha")

    code, context = _recovery(caught.value)
    assert code == "route_target_conflict"
    assert context["project_id"] == "alpha"
    assert context["requested_target"] == str(moved)
    assert context["descriptor_target"] == str(original)
    assert context["classification"] == "probable_move"
    assert Path(context["config_path"]) == replay.config
    assert Path(context["artifact_root"]).name == "alpha"


# ── #24 competing writer during restore ──────────────────────────────────────


def test_competing_writer_destination_survives_restore(replay, databricks):
    created = replay.create_project("alpha")
    _closed_task_snapshot(created["anchor"], replay)
    replay.new_compute()
    competitor: dict[str, Path] = {}

    def compete(point):
        # Another writer creates the restore destination immediately before the
        # restore's first mutation of it, after every preflight absence check.
        destination = Path(point.path)
        if "sentinel" in competitor or destination.parts[-2:] != ("workspace", "projects"):
            return
        destination.mkdir(parents=True)
        sentinel = destination / "competing-writer.txt"
        sentinel.write_text("owned by another writer\n")
        competitor["sentinel"] = sentinel

    with CrashInjector(scope=[databricks.local_root], hook=compete), pytest.raises(Exception) as caught:
        replay.bootstrap("alpha")

    assert "sentinel" in competitor, "restore never reached artifact publication"
    assert competitor["sentinel"].is_file(), "restore cleanup deleted the competing writer's directory"
    assert competitor["sentinel"].read_text() == "owned by another writer\n"
    assert _recovery(caught.value)[0] == "restore_destination_conflict"


# ── restore qualification ────────────────────────────────────────────────────


def test_wrong_durable_root_is_unavailable_not_first_use(replay, databricks):
    target = replay.target("alpha")
    created = replay.create_project("alpha", target)
    _closed_task_snapshot(created["anchor"], replay)
    document = importlib.import_module("odibi_anchor.portfolio").load_portfolio_document(replay.config)
    replay.write_portfolio(
        {"alpha": str(target)}, durable_root="/Volumes/main/anchor/stale/anchor",
        expected_sha256=document["sha256"],
    )
    replay.new_compute()

    with pytest.raises(Exception) as caught:
        replay.bootstrap("alpha")

    assert _recovery(caught.value)[0] == "durable_root_unavailable"
    assert not any((root / ".agent_memory.db").exists() for root in databricks.runtime_roots())


def test_first_use_restore_is_classified_no_lineage(replay):
    replay.write_portfolio({"alpha": str(replay.target("alpha"))})

    result = replay.bootstrap("alpha")

    assert result["startup_packet"]["status"] == "ready"
    assert result["preparation"]["restore"]["classification"] == "no_lineage"


def test_database_publication_failure_is_restore_incomplete(replay, databricks):
    created = replay.create_project("alpha")
    _closed_task_snapshot(created["anchor"], replay)
    replay.new_compute()

    def database_publication(point):
        return point.operation == "os.link" and point.path.endswith(".agent_memory.db")

    def publication_failure(point):
        return OSError(errno.EIO, "injected database publication failure", point.path)

    # The failing restore already reports the owned partial state as restore_incomplete,
    # chained from the injected OSError, instead of surfacing the raw OSError.
    with CrashInjector(
        scope=[databricks.local_root], crash_if=database_publication, error=publication_failure,
    ) as injector, pytest.raises(RuntimeError) as failed:
        replay.bootstrap("alpha")
    assert injector.crashed is not None, "restore never reached database publication"
    assert _recovery(failed.value)[0] == "restore_incomplete"
    assert isinstance(failed.value.__cause__, OSError)

    replay.new_process()
    with pytest.raises(Exception) as caught:
        replay.bootstrap("alpha")

    code, _context = _recovery(caught.value)
    assert code == "restore_incomplete"
    operations = json.dumps(getattr(caught.value, "next_operations", []))
    assert "resume" in operations
    assert "abandon" in operations


# ── host guidance through the fake Workspace ─────────────────────────────────


def test_host_guidance_and_bootstrap_succeed_under_workspace_latency(replay, databricks):
    workspace_root = "/Users/henry@example.invalid/anchor-host"
    databricks.workspace.add_directory(workspace_root)
    databricks.set_latency(0.002)
    setup_host = importlib.import_module("odibi_anchor.host_setup").setup_host

    installed = setup_host(f"/Workspace{workspace_root}", adapter="databricks")
    repeated = setup_host(f"/Workspace{workspace_root}", adapter="databricks")
    created = replay.create_project("alpha")

    assert installed["status"] == "installed"
    assert repeated["status"] == "unchanged"
    assert f"{workspace_root}/.odibi-anchor-host-guidance.json" in databricks.workspace.files
    assert f"{workspace_root}/agent_bootstrap.py" in databricks.workspace.files
    assert databricks.clients[0].config.settings == {
        "http_timeout_seconds": 30, "retry_timeout_seconds": 60,
    }
    assert created["startup_packet"]["status"] == "ready"
    assert created["startup_packet"]["durable_checkpoint"]["action"] == "created"


def test_startup_packet_reports_phase_timings(replay, databricks):
    databricks.set_latency(0.002)
    replay.create_project("alpha")
    replay.new_compute()

    packet = replay.bootstrap("alpha")["startup_packet"]

    timings = packet["timings"]
    assert (timings["schema"], timings["unit"]) == ("odibi-anchor-bootstrap-timings-v1", "ms")
    phases = {entry["phase"]: entry for entry in timings["phases"]}
    assert {"host_guidance", "local_state_identity", "runtime_preparation", "init"} <= set(phases)
    assert all(entry["outcome"] == "ok" and entry["elapsed_ms"] >= 0 for entry in phases.values())
    restore = [item for item in phases["runtime_preparation"].get("sub_phases", [])
               if item["phase"] == "restore"]
    assert [item["outcome"] for item in restore] == ["ok"]
    assert timings["total_elapsed_ms"] >= sum(entry["elapsed_ms"] for entry in phases.values())


def test_recovery_codes_survive_bootstrap_phase_timing(replay):
    created = replay.create_project("alpha")
    runtime_root = Path(created["startup_packet"]["local_state"]["runtime_root"])
    replay.new_process()
    displaced = runtime_root.with_name(runtime_root.name + ".displaced")
    runtime_root.rename(displaced)
    runtime_root.symlink_to(displaced, target_is_directory=True)

    with pytest.raises(RuntimeError) as caught:
        replay.bootstrap("alpha")

    code, context = _recovery(caught.value)
    assert code == "databricks_local_state_identity_collision"
    assert (context["runtime_root"], context["runtime_root_status"]) == (str(runtime_root), "symlink")
    timings = getattr(caught.value, "bootstrap_timings", None)
    assert timings is not None, "failed bootstrap must still report its phase timings"
    phases = {entry["phase"]: entry for entry in timings["phases"]}
    assert phases["local_state_identity"]["outcome"] == "raised"
    assert "runtime_preparation" not in phases


# ── #28 path arguments through the MCP gateway ───────────────────────────────


def _git(target: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "-c", "user.name=Replay", "-c",
         "user.email=replay@example.invalid", *args],
        cwd=target, check=True, capture_output=True,
    )


@pytest.fixture
def mcp_gateway(tmp_path, monkeypatch):
    """The in-process MCP gateway bound to a local managed project with a Git target."""
    state = tmp_path / "state"
    target = tmp_path / "repository"
    state.mkdir()
    (target / "results").mkdir(parents=True)
    (target / "results" / "manifest.json").write_text('[{"run": 1, "score": 0.5}]\n')
    _git(target, "init", "-q", "-b", "main")
    _git(target, "add", ".")
    _git(target, "commit", "-q", "-m", "fixture")
    importlib.import_module("odibi_anchor.startup").register_project(
        anchor_home=state, project_id="alpha", project_root=target,
    )
    for name, value in {
        "ANCHOR_HOME": state, "ANCHOR_MEMORY_DB": state / ".agent_memory.db",
        "ANCHOR_PROJECT_ID": "alpha", "ANCHOR_PROJECT_ROOT": target,
        "ANCHOR_TRUST_DOMAIN": "work",
    }.items():
        monkeypatch.setenv(name, str(value))
    server = importlib.import_module("odibi_anchor.mcp_server")
    for name in ("_CW", "_ROOT", "_ROUTE_BINDING"):
        monkeypatch.setattr(server, name, None)

    def call(action: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        payload = {"args": list(args), **kwargs} if args else kwargs
        envelope = json.loads(server.anchor_execute(
            action, json.dumps(payload), response_version=2, response_detail="full",
        ))
        if not envelope["ok"]:
            raise AssertionError(f"MCP {action} failed: {envelope['error']}")
        return envelope["result"]

    yield call, target
    server._table_cache.clear()


@pytest.mark.parametrize("pandas_available", [False, True], ids=["pandas_absent", "pandas_present"])
def test_mcp_touched_preserves_json_path(mcp_gateway, workflow, monkeypatch, pandas_available):
    call, target = mcp_gateway
    path = "results/manifest.json"
    call("status")
    call("audit_history")
    workflow.accept(call, workflow.plan("source_change", [path]), name="manifest")
    (target / path).write_text('[{"run": 1, "score": 0.75}]\n')
    if not pandas_available:
        monkeypatch.setitem(sys.modules, "pandas", None)

    call("touched", path)

    assert path in call("status", output_format="dict")["samples"]["files_changed"]
