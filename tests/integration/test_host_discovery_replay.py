"""#30 replay: portfolio discovery, host-guidance drift and the fresh-compute doctor plan.

Scenarios drive public entry points on the fake Databricks runtime and assert the
error-code contract and durable facts, never message text.
"""
from __future__ import annotations

import importlib
import importlib.metadata
import json
import runpy
import shutil
from pathlib import Path
from typing import Any, cast

import pytest

from tests.fixtures.fake_databricks import DEFAULT_VOLUME

_REMOTE_MUTATIONS = {"upload", "mkdirs", "delete", "upload_from", "create_directory"}


def _module(name: str) -> Any:
    # bootstrap.init evicts odibi_anchor modules; always resolve the current module.
    return importlib.import_module(name)


def _doctor(replay: Any, project_id: str = "alpha") -> dict[str, Any]:
    return _module("odibi_anchor.startup").doctor(
        fresh_compute=True, config_path=replay.config, host_id="databricks",
        project_id=project_id,
    )


def _closed_task_snapshot(anchor: Any, replay: Any) -> dict[str, Any]:
    replay.start_analysis_task(anchor)
    closure = anchor("learning", "assess", outcome="nothing_reusable_learned", output_format="dict")
    assert closure["accepted_task_closure"]["status"] == "closed"
    return closure["durable_state"]


def _persistent_state(replay: Any, databricks: Any) -> dict[str, Any]:
    """Every byte a read-only plan must leave untouched: Workspace FUSE, Volumes, local disk."""
    def tree(root: Path) -> dict[str, bytes | None]:
        if not root.exists():
            return {}
        return {
            path.relative_to(root).as_posix(): path.read_bytes() if path.is_file() else None
            for path in sorted(root.rglob("*"))
        }

    return {
        "workspace_fuse": tree(replay.root / "workspace-fuse"),
        "volumes": tree(databricks.root / "volumes"),
        "compute_local": tree(databricks.local_root),
        "workspace_api": dict(databricks.workspace.files),
    }


def _steps(plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {step["step"]: step for step in plan["steps"]}


def _next_operations(plan: dict[str, Any]) -> list[dict[str, Any]]:
    return [step["next_operation"] for step in plan["steps"] if step.get("next_operation")]


def test_fresh_compute_doctor_plans_restore_and_route_without_writing(replay, databricks):
    target = replay.target("alpha")
    created = replay.create_project("alpha", target)
    snapshot = _closed_task_snapshot(created["anchor"], replay)
    replay.new_compute()
    before = _persistent_state(replay, databricks)
    calls_before = len(databricks.calls)

    plan = _doctor(replay)

    assert _persistent_state(replay, databricks) == before
    assert not [call for call in databricks.calls[calls_before:] if call[1] in _REMOTE_MUTATIONS]
    steps = _steps(plan)
    assert [step["step"] for step in plan["steps"]] == [
        "portfolio_discovery", "host_guidance", "durable_lineage", "route_comparison",
        "launch_inputs",
    ]
    assert (plan["status"], plan["read_only"]) == ("ready", True)
    assert steps["portfolio_discovery"]["status"] == "ok"
    assert steps["host_guidance"]["status"] == "ok"
    lineage = steps["durable_lineage"]
    assert (lineage["classification"], lineage["snapshot"]["snapshot_id"]) == (
        "restored", snapshot["manifest"]["snapshot_id"],
    )
    route = steps["route_comparison"]
    assert (route["classification"], route["descriptor_target"], route["portfolio_target"]) == (
        "match", str(target), str(target),
    )
    assert route["descriptor_source"] == f"snapshot:{snapshot['manifest']['snapshot_id']}"
    launch = steps["launch_inputs"]
    assert launch["init_globals"] == {"ANCHOR_PROJECT_ID": "alpha"}
    assert plan["next_operation"]["operation"] == "run_managed_launcher"
    # Never a suggestion to carry state from another compute's local disk.
    assert not any(
        str(databricks.local_root) in json.dumps(operation) for operation in _next_operations(plan)
    )

    restored = replay.bootstrap("alpha")
    assert restored["startup_packet"]["restore"]["snapshot_id"] == lineage["snapshot"]["snapshot_id"]
    after_launch = _steps(_doctor(replay))
    assert after_launch["durable_lineage"]["classification"] == "local_present"
    assert after_launch["route_comparison"]["descriptor_source"] == "local_state"
    assert after_launch["route_comparison"]["classification"] == "match"


def test_fresh_compute_doctor_accepts_a_descriptor_with_defaulted_route_fields(replay, databricks):
    # 0.3.23 descriptors may omit id and project_type; 0.3.25 defaults them from the
    # managed directory and the artifact root, while target_root stays mandatory.
    target = replay.target("alpha")
    created = replay.create_project("alpha", target)
    descriptor = Path(created["startup_packet"]["artifact_root"]) / "PROJECT.md"
    lines = descriptor.read_text().split("\n")
    frontmatter_end = lines.index("---", 1)
    descriptor.write_text("\n".join(
        line for number, line in enumerate(lines)
        if not (0 < number < frontmatter_end and line.startswith(("id:", "project_type:")))
    ))
    replay.snapshot(created)
    replay.new_compute()
    before = _persistent_state(replay, databricks)

    plan = _doctor(replay)

    assert _persistent_state(replay, databricks) == before
    route = _steps(plan)["route_comparison"]
    assert route["descriptor_source"].startswith("snapshot:")
    assert (route["status"], route["classification"], route["descriptor_target"]) == (
        "ok", "match", str(target),
    )
    assert route["descriptor_defaulted_fields"] == ["id", "project_type"]
    assert route["descriptor_project_type"] == "referenced"
    assert plan["status"] == "ready"

    assert replay.bootstrap("alpha")["startup_packet"]["status"] == "ready"
    local = _steps(_doctor(replay))["route_comparison"]
    assert (local["descriptor_source"], local["classification"]) == ("local_state", "match")
    assert local["descriptor_defaulted_fields"] == ["id", "project_type"]


def test_fresh_compute_doctor_classifies_a_probable_target_move(replay, databricks):
    old_target = replay.target("alpha")
    created = replay.create_project("alpha", old_target)
    _closed_task_snapshot(created["anchor"], replay)
    new_target = replay.root / "workspace-fuse" / "targets" / "projects" / "alpha"
    new_target.parent.mkdir(parents=True)
    shutil.move(str(old_target), str(new_target))
    document = _module("odibi_anchor.portfolio").load_portfolio_document(replay.config)
    replay.write_portfolio({"alpha": str(new_target)}, expected_sha256=document["sha256"])
    replay.new_compute()
    before = _persistent_state(replay, databricks)

    plan = _doctor(replay)

    assert _persistent_state(replay, databricks) == before
    route = _steps(plan)["route_comparison"]
    assert (route["status"], route["classification"]) == ("blocked", "probable_move")
    assert route["error"]["error_code"] == "route_target_conflict"
    assert (route["descriptor_target"], route["portfolio_target"]) == (
        str(old_target), str(new_target),
    )
    assert (plan["status"], plan["next_step"]) == ("blocked", "route_comparison")
    assert plan["next_operation"]["requires_owner"] is True
    with pytest.raises(ValueError) as raised:
        replay.bootstrap("alpha")
    assert cast(Any, raised.value).error_code == "route_target_conflict"


def test_fresh_compute_doctor_classifies_first_use_and_missing_lineage(replay, databricks):
    replay.write_portfolio({"alpha": str(replay.target("alpha"))})

    first_use = _steps(_doctor(replay))

    assert first_use["durable_lineage"]["classification"] == "no_lineage"
    assert first_use["route_comparison"]["classification"] == "unregistered"

    created = replay.bootstrap("alpha")
    _closed_task_snapshot(created["anchor"], replay)
    replay.new_compute()
    shutil.rmtree(databricks.volume_path(f"{DEFAULT_VOLUME}/anchor/alex/snapshots"))

    missing = _doctor(replay)

    lineage = _steps(missing)["durable_lineage"]
    assert (lineage["status"], lineage["classification"]) == ("blocked", "durable_lineage_missing")
    assert lineage["error"]["error_code"] == "durable_lineage_missing"
    assert lineage["next_operation"]["retry_safety"] == "read_only"
    assert _steps(missing)["route_comparison"]["status"] == "not_evaluated"
    assert missing["next_step"] == "durable_lineage"


def test_fresh_compute_doctor_reports_an_unavailable_durable_root(replay):
    replay.write_portfolio(
        {"alpha": str(replay.target("alpha"))}, durable_root="/Volumes/main/missing/state/anchor",
    )

    lineage = _steps(_doctor(replay))["durable_lineage"]

    assert (lineage["status"], lineage["classification"]) == ("blocked", "durable_root_unavailable")
    assert lineage["error"]["error_code"] == "durable_root_unavailable"


def test_host_guidance_drift_blocks_bootstrap_until_explicit_reconcile(replay):
    created = replay.create_project("alpha")
    _closed_task_snapshot(created["anchor"], replay)
    edited = replay.instruction_root / ".assistant" / "README.md"
    edited.write_text("team edit\n")
    replay.new_process()

    with pytest.raises(RuntimeError) as raised:
        replay.bootstrap("alpha")

    error = cast(Any, raised.value)
    assert error.error_code == "host_guidance_drift"
    assert [(entry["path"], entry["classification"]) for entry in error.context["files"]] == [
        (".assistant/README.md", "unmanaged_edit"),
    ]
    assert edited.read_text() == "team edit\n"
    guidance = _steps(_doctor(replay))["host_guidance"]
    assert guidance["status"] == "blocked"
    assert guidance["next_operation"]["arguments"]["approve_replace_edited"] is True

    setup_host = _module("odibi_anchor.host_setup").setup_host
    reconciled = setup_host(
        replay.instruction_root, adapter="databricks", reconcile=True, dry_run=False,
        approve_replace_edited=True,
    )
    assert (Path(reconciled["backup"]["root"]) / ".assistant" / "README.md").read_text() == "team edit\n"
    assert replay.bootstrap("alpha")["startup_packet"]["status"] == "ready"


def test_launcher_finds_a_portfolio_outside_the_instruction_root_through_the_sidecar(
    replay, databricks,
):
    # The reported layout: the portfolio lives above the instruction root, not below it.
    replay.config = replay.root / "home" / ".odibi-anchor" / "anchor.toml"
    replay.config.parent.mkdir(parents=True)
    replay.write_portfolio({"alpha": str(replay.target("alpha"))})
    setup_host = _module("odibi_anchor.host_setup").setup_host
    setup_host(replay.instruction_root, adapter="databricks")
    launcher = replay.instruction_root / ".assistant" / "agent_bootstrap.py"
    # An exact pin skips the package index; the launcher still verifies the installed version.
    init_globals = {
        "ANCHOR_PROJECT_ID": "alpha",
        "ANCHOR_PACKAGE_VERSION": importlib.metadata.version("odibi-anchor"),
    }

    with pytest.raises(RuntimeError) as raised:
        runpy.run_path(str(launcher), init_globals=dict(init_globals))
    error = cast(Any, raised.value)
    assert error.error_code == "managed_portfolio_not_found"
    assert error.context["selected_path"] == str(
        replay.instruction_root.resolve() / ".odibi-anchor" / "anchor.toml"
    )
    discovery = _steps(_doctor(replay))["portfolio_discovery"]
    assert (discovery["status"], discovery["selects_config"]) == ("action_required", False)
    assert "--portfolio" in discovery["next_operation"]["copy_ready"]

    recorded = setup_host(
        replay.instruction_root, adapter="databricks", portfolio_config=replay.config,
    )
    replay.new_process()
    namespace = runpy.run_path(str(launcher), init_globals=dict(init_globals))

    assert recorded["host_binding"]["binding"]["host_id"] == "databricks"
    packet = namespace["STARTUP_PACKET"]
    assert (packet["status"], packet["project_id"], packet["host_id"]) == (
        "ready", "alpha", "databricks",
    )
    discovery_phase = next(
        phase for phase in packet["timings"]["phases"] if phase["phase"] == "portfolio_discovery"
    )
    assert discovery_phase["source"].startswith("host binding sidecar")
    assert _steps(_doctor(replay))["portfolio_discovery"]["status"] == "ok"
