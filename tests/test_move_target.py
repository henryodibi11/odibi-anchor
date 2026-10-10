"""Contract tests for ``anchor portfolio move-target`` on a local (non-Databricks) host.

Crash injection over every mutation point and fresh-compute restoration live in
``tests/integration/test_move_target_replay.py``.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import os
from pathlib import Path
from typing import Any

import pytest

from tests.fixtures.fake_databricks.crash import CrashInjector, InjectedCrash

HOST = "local"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tree(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): _sha(path)
        for path in sorted(root.rglob("*")) if path.is_file() and not path.is_symlink()
    }


def _code(exc: BaseException) -> tuple[str | None, dict[str, Any]]:
    return getattr(exc, "error_code", None), dict(getattr(exc, "context", {}) or {})


def _module(name: str) -> Any:
    # ``launch`` re-imports odibi_anchor; always call the current module.
    return importlib.import_module(name)


class Deployment:
    """A portfolio-managed local host with launched projects."""

    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch, projects: dict[str, Path]) -> None:
        self.root = root
        self.config = root / "anchor.toml"
        self.state = root / "state"
        self.durable = root / "durable"
        self.durable.mkdir(parents=True)
        for name in [name for name in os.environ if name.startswith("ANCHOR_")]:
            monkeypatch.delenv(name)
        for name in ("ANCHOR_PROJECT_ID", "ANCHOR_PROJECT_ROOT", "ANCHOR_HOME"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("ANCHOR_MEMORY_DB", str(self.state / ".agent_memory.db"))
        self.monkeypatch = monkeypatch
        self.write_portfolio({project: str(target) for project, target in projects.items()})
        for project, target in projects.items():
            _module("odibi_anchor.startup").prepare_portfolio_runtime(
                config_path=self.config, host_id=HOST, project_id=project,
            )
            self.launch(project, target)

    def write_portfolio(self, targets: dict[str, str]) -> None:
        payload = {
            "schema_version": 1,
            "authority": {"id": "owner", "trust_domain": "work"},
            "hosts": {HOST: {"adapter": "amp", "local_state_root": str(self.state),
                             "durable_root": str(self.durable)}},
            "projects": {project: {"targets": {HOST: target}} for project, target in targets.items()},
            "personas": {},
        }
        expected = _sha(self.config) if self.config.exists() else None
        _module("odibi_anchor.portfolio").write_portfolio(self.config, payload, expected_sha256=expected)

    def launch(self, project: str, target: Path) -> Any:
        for name in ("ANCHOR_PROJECT_ID", "ANCHOR_PROJECT_ROOT"):
            self.monkeypatch.delenv(name, raising=False)
        return _module("odibi_anchor.startup").launch(anchor_home=self.state, project_id=project, project_root=target)

    def artifact(self, project: str = "alpha") -> Path:
        return self.state / "workspace" / "projects" / project

    def move(self, project: str = "alpha", *, source: Path, destination: Path, **kwargs: Any) -> dict[str, Any]:
        return _module("odibi_anchor").move_target(
            config_path=self.config, host_id=HOST, project_id=project,
            from_target=str(source), to_target=str(destination), **kwargs,
        )

    def prepare(self, project: str = "alpha") -> dict[str, Any]:
        for name in ("ANCHOR_PROJECT_ID", "ANCHOR_PROJECT_ROOT"):
            self.monkeypatch.delenv(name, raising=False)
        return _module("odibi_anchor.startup").prepare_portfolio_runtime(
            config_path=self.config, host_id=HOST, project_id=project,
        )


@pytest.fixture
def targets(tmp_path: Path) -> tuple[Path, Path]:
    old, new = tmp_path / "workspace" / "alpha", tmp_path / "workspace" / "projects" / "alpha"
    old.mkdir(parents=True)
    new.mkdir(parents=True)
    return old, new


@pytest.fixture
def deployment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, targets: tuple[Path, Path]) -> Deployment:
    return Deployment(tmp_path, monkeypatch, {"alpha": targets[0]})


def test_move_updates_every_authority_and_starts_a_new_owner_epoch(deployment, targets):
    old, new = targets
    artifact = deployment.artifact()
    owner = artifact / "continuity" / "v1" / "OWNER.json"
    before = {"portfolio": _sha(deployment.config), "descriptor": _sha(artifact / "PROJECT.md"),
              "owner": _sha(owner)}
    original_descriptor = (artifact / "PROJECT.md").read_bytes()

    result = deployment.move(source=old, destination=new)

    assert result["status"] == "completed"
    assert result["hashes"]["before"]["portfolio_sha256"] == before["portfolio"]
    assert result["hashes"]["before"]["descriptor_sha256"] == before["descriptor"]
    assert result["hashes"]["before"]["owner_sha256"] == before["owner"]
    assert result["hashes"]["after"]["portfolio_sha256"] == _sha(deployment.config) != before["portfolio"]
    assert result["artifact_root"] == str(artifact)
    portfolio = _module("odibi_anchor.portfolio").load_portfolio(deployment.config)
    assert portfolio["projects"]["alpha"]["targets"][HOST] == str(new)
    descriptor = _module("odibi_anchor._dispatcher._descriptor").read_descriptor(artifact)
    assert descriptor.fields["target_root"] == str(new)
    # Only the route line changed; the original bytes and owner epoch are preserved.
    archive = artifact / "continuity" / "archive" / result["migration_id"]
    assert not owner.exists() and _sha(archive / "OWNER.json") == before["owner"]
    journal = json.loads(Path(result["journal_path"]).read_bytes())
    assert journal["state"] == "completed"
    assert (artifact / journal["backups"]["descriptor"]).read_bytes() == original_descriptor
    assert _sha(artifact / journal["backups"]["portfolio"]) == before["portfolio"]
    receipt = json.loads(Path(result["receipt_path"]).read_bytes())
    assert (receipt["from_target"], receipt["to_target"]) == (str(old), str(new))
    assert {journal["steps"][name]["status"] for name in ("pre_snapshot", "pending_snapshot", "post_snapshot")} == {"done"}

    assert deployment.prepare()["target_root"] == str(new)
    runtime = deployment.launch("alpha", new)
    binding = runtime("status", output_format="dict")["runtime"]["route_binding"]
    assert (binding["target_root"], binding["artifact_root"]) == (str(new), str(artifact))
    assert json.loads(owner.read_bytes())["target_root"] == str(new)


def test_dry_run_reports_hashes_and_writes_nothing(deployment, targets, tmp_path):
    old, new = targets
    before = _tree(tmp_path)

    preview = deployment.move(source=old, destination=new, dry_run=True)

    assert _tree(tmp_path) == before
    assert preview["status"] == "ready" and preview["start_state"] == "aligned"
    assert preview["hashes"] == {
        "portfolio_sha256": _sha(deployment.config),
        "descriptor_sha256": _sha(deployment.artifact() / "PROJECT.md"),
        "owner_sha256": _sha(deployment.artifact() / "continuity" / "v1" / "OWNER.json"),
    }
    assert preview["quiescence"] == {"open_task_windows": [], "non_terminal_workflows": [], "unverifiable": []}
    assert "--dry-run" not in preview["next_operation"]["copy_ready"]


def test_rerun_after_completion_is_idempotent(deployment, targets, tmp_path):
    old, new = targets
    first = deployment.move(source=old, destination=new)
    before = _tree(tmp_path)

    again = deployment.move(source=old, destination=new)

    assert again["status"] == "already_migrated"
    assert again["receipt"]["migration_id"] == first["migration_id"]
    assert _tree(tmp_path) == before


def test_batch_preflights_everything_then_moves_each_project(tmp_path, monkeypatch):
    sources = {name: tmp_path / "old" / name for name in ("alpha", "beta")}
    destinations = {name: tmp_path / "projects" / name for name in ("alpha", "beta")}
    for path in [*sources.values(), *destinations.values()]:
        path.mkdir(parents=True)
    deployment = Deployment(tmp_path, monkeypatch, sources)
    mapping = tmp_path / "moves.json"
    mapping.write_text(json.dumps({"moves": [
        {"project": name, "from": str(sources[name]), "to": str(destinations[name])} for name in sources
    ]}), encoding="utf-8")
    migration = _module("odibi_anchor._migration")
    moves = migration.load_mapping(mapping)
    before = _tree(tmp_path)

    preview = migration.move_targets(config_path=deployment.config, host_id=HOST, moves=moves, dry_run=True)
    assert _tree(tmp_path) == before and [item["status"] for item in preview["moves"]] == ["ready", "ready"]
    clashing = [{**moves[0]}, {**moves[1], "to": moves[0]["to"]}]
    with pytest.raises(Exception) as refused:
        migration.move_targets(config_path=deployment.config, host_id=HOST, moves=clashing)
    assert _code(refused.value)[0] == "target_migration_blocked"
    assert _tree(tmp_path) == before

    applied = migration.move_targets(config_path=deployment.config, host_id=HOST, moves=moves)

    assert [item["status"] for item in applied["moves"]] == ["completed", "completed"]
    portfolio = _module("odibi_anchor.portfolio").load_portfolio(deployment.config)
    assert {name: portfolio["projects"][name]["targets"][HOST] for name in sources} == {
        name: str(destinations[name]) for name in sources
    }


def test_private_scratch_target_is_a_valid_destination(deployment, targets, tmp_path):
    old, _new = targets
    scratch = tmp_path / "workspace" / "projects" / "_scratch"
    scratch.mkdir(parents=True)

    result = deployment.move(source=old, destination=scratch)

    assert result["status"] == "completed"
    assert deployment.prepare()["target_root"] == str(scratch)


def test_old_target_removed_after_a_manual_portfolio_move_is_recovered(deployment, targets):
    old, new = targets
    deployment.write_portfolio({"alpha": str(new)})
    old.rmdir()
    with pytest.raises(Exception) as conflict:
        deployment.prepare()
    code, context = _code(conflict.value)
    assert (code, context["classification"]) == ("route_target_conflict", "probable_move")
    assert context["supported_move_available"] is True

    result = deployment.move(source=old, destination=new)

    assert (result["status"], result["start_state"]) == ("completed", "portfolio_already_moved")
    assert result["steps"]["portfolio"]["status"] == "already_applied"
    assert deployment.prepare()["target_root"] == str(new)


@pytest.mark.parametrize("damage", [
    "# Alpha\n\nNo frontmatter.\n",
    "---\nid: alpha\nproject_type: referenced\ntarget_root: /x\nid: again\n---\n",
])
def test_damaged_descriptor_is_refused_and_never_rewritten(deployment, targets, damage):
    old, new = targets
    descriptor = deployment.artifact() / "PROJECT.md"
    descriptor.write_text(damage, encoding="utf-8")
    portfolio_before = _sha(deployment.config)

    with pytest.raises(Exception) as refused:
        deployment.move(source=old, destination=new)

    code, context = _code(refused.value)
    assert (code, context["classification"]) == ("target_migration_blocked", "state_mismatch")
    assert context["descriptor_damage"]["integrity_status"] in {"missing_frontmatter", "malformed_frontmatter"}
    assert descriptor.read_text(encoding="utf-8") == damage
    assert _sha(deployment.config) == portfolio_before
    assert not (deployment.artifact() / "migrations").exists()


@pytest.mark.parametrize("case", ["missing", "symlink", "inside_anchor_home", "other_project", "file"])
def test_unsuitable_destination_is_refused(tmp_path, monkeypatch, case):
    old, other = tmp_path / "old" / "alpha", tmp_path / "old" / "beta"
    old.mkdir(parents=True)
    other.mkdir(parents=True)
    deployment = Deployment(tmp_path, monkeypatch, {"alpha": old, "beta": other})
    destination = {
        "missing": tmp_path / "absent",
        "symlink": tmp_path / "link",
        "inside_anchor_home": deployment.state / "workspace" / "inside",
        "other_project": other,
        "file": tmp_path / "plain-file",
    }[case]
    if case == "symlink":
        destination.symlink_to(old.parent)
    elif case == "inside_anchor_home":
        destination.mkdir(parents=True)
    elif case == "file":
        destination.write_text("x", encoding="utf-8")

    with pytest.raises(Exception) as refused:
        deployment.move(source=old, destination=destination)

    code, context = _code(refused.value)
    assert (code, context["classification"]) == ("target_migration_blocked", "destination_unsuitable")
    assert context["problems"]
    assert not (deployment.artifact() / "migrations").exists()


def _before_portfolio_write(deployment: Deployment):
    """Crash predicate: the portfolio replacement's temporary file is about to be created."""
    parent = str(deployment.config.parent.resolve())
    return lambda point: point.operation == "os.open" and point.path.startswith(parent + os.sep + ".anchor.toml.")


def test_concurrent_portfolio_change_is_refused_and_rollback_converges(deployment, targets):
    old, new = targets
    artifact = deployment.artifact()
    descriptor_before = _sha(artifact / "PROJECT.md")
    manifests = []

    def competing_writer(point):
        # Another writer changes an unrelated host field while the pending snapshot publishes.
        if point.path.endswith(".manifest.json"):
            manifests.append(point)
            if len(manifests) == 2:
                document = _module("odibi_anchor.portfolio").load_portfolio(deployment.config)
                document["hosts"][HOST]["instruction_root"] = str(deployment.root)
                _module("odibi_anchor.portfolio").write_portfolio(deployment.config, document)

    with CrashInjector(scope=[deployment.root], hook=competing_writer), pytest.raises(Exception) as refused:
        deployment.move(source=old, destination=new)

    code, context = _code(refused.value)
    assert (code, context["classification"], context["store"]) == ("target_migration_blocked", "concurrent_change", "portfolio")
    foreign = _sha(deployment.config)
    assert foreign not in {context["expected"]["pre_sha256"], context["expected"]["after_sha256"]}
    assert refused.value.next_operations[0]["arguments"].get("rollback") is True
    # A fresh route resolution recognizes the journaled intermediate state.
    with pytest.raises(Exception) as conflict:
        deployment.prepare()
    conflict_code, conflict_context = _code(conflict.value)
    assert (conflict_code, conflict_context["classification"]) == ("route_target_conflict", "migration_pending")
    assert conflict_context["migration"]["migration_id"] == context["migration_id"]
    assert {"--resume", "--rollback"} <= {op["copy_ready"].split()[-1] for op in conflict.value.next_operations}

    rolled_back = deployment.move(source=old, destination=new, rollback=True)

    assert rolled_back["status"] == "rolled_back"
    assert _sha(deployment.config) == foreign  # the competing write is preserved
    assert _sha(artifact / "PROJECT.md") == descriptor_before
    assert (artifact / "continuity" / "v1" / "OWNER.json").is_file()
    assert deployment.prepare()["target_root"] == str(old)


def test_concurrent_descriptor_change_stops_without_guessing(deployment, targets):
    old, new = targets
    descriptor = deployment.artifact() / "PROJECT.md"
    foreign = descriptor.read_text(encoding="utf-8") + "\nEdited concurrently.\n"
    edited = []

    def competing_writer(point):
        if not edited and point.path.endswith("PROJECT.md.pre"):
            edited.append(point)
            descriptor.write_text(foreign, encoding="utf-8")

    with CrashInjector(scope=[deployment.root], hook=competing_writer), pytest.raises(Exception) as refused:
        deployment.move(source=old, destination=new)
    code, context = _code(refused.value)
    assert (code, context["classification"], context["store"]) == ("target_migration_blocked", "concurrent_change", "descriptor")
    assert descriptor.read_text(encoding="utf-8") == foreign

    with pytest.raises(Exception) as rollback:
        deployment.move(source=old, destination=new, rollback=True)

    assert _code(rollback.value)[1]["store"] == "descriptor"
    assert descriptor.read_text(encoding="utf-8") == foreign


def test_crash_before_portfolio_write_is_migration_pending_until_resumed(deployment, targets):
    old, new = targets
    injector = CrashInjector(scope=[deployment.root], crash_if=_before_portfolio_write(deployment))
    with injector, pytest.raises(BaseException) as crashed:
        deployment.move(source=old, destination=new)
    assert injector.crashed is not None and type(crashed.value).__name__ == "InjectedCrash"
    with pytest.raises(Exception) as conflict:
        deployment.prepare()
    assert _code(conflict.value)[1]["classification"] == "migration_pending"
    with pytest.raises(Exception) as fresh:
        deployment.move(source=old, destination=new)
    assert _code(fresh.value)[0] == "target_migration_incomplete"

    resumed = deployment.move(source=old, destination=new, resume=True)

    assert resumed["status"] == "completed"
    assert deployment.prepare()["target_root"] == str(new)


def test_workflow_bound_to_prior_target_names_the_receipt(deployment, targets):
    old, new = targets
    workflow = _module("odibi_anchor.codebase._workflow")
    database = deployment.state / ".agent_memory.db"
    owner = {"project_id": "alpha", "target_root": str(old), "artifact_root": str(deployment.artifact()),
             "anchor_home": str(deployment.state.resolve()), "trust_domain": "work"}
    plan = {"schema_version": 1, "goal": "Historical work.", "risk": "low", "execution_mode": "read_only"}
    created = workflow.create_workflow(database, owner=owner, request_id="history", plan=plan)
    workflow.transition_workflow(database, owner=owner, workflow_id=created["workflow_id"], expected_generation=0,
                                 request_id="history-cancel", operation="cancel", payload={"reason": "done"})
    result = deployment.move(source=old, destination=new)

    with pytest.raises(workflow.WorkflowError) as caught:
        workflow.read_workflow(database, owner={**owner, "target_root": str(new)}, workflow_id=created["workflow_id"])

    assert caught.value.code == "unavailable"
    code, context = _code(caught.value)
    assert code == "workflow_bound_to_prior_target"
    assert (context["prior_target"], context["migration_id"]) == (str(old), result["migration_id"])
    with pytest.raises(workflow.WorkflowError) as unrelated:
        workflow.read_workflow(database, owner={**owner, "target_root": str(old.parent)}, workflow_id=created["workflow_id"])
    assert getattr(unrelated.value, "error_code", None) is None


def test_non_terminal_workflow_blocks_with_its_exact_id(deployment, targets):
    old, new = targets
    workflow = _module("odibi_anchor.codebase._workflow")
    owner = {"project_id": "alpha", "target_root": str(old), "artifact_root": str(deployment.artifact()),
             "anchor_home": str(deployment.state.resolve()), "trust_domain": "work"}
    plan = {"schema_version": 1, "goal": "Live work.", "risk": "low", "execution_mode": "read_only"}
    live = workflow.create_workflow(deployment.state / ".agent_memory.db", owner=owner, request_id="live", plan=plan)

    with pytest.raises(Exception) as refused:
        deployment.move(source=old, destination=new)

    code, context = _code(refused.value)
    assert (code, context["classification"]) == ("target_migration_blocked", "not_quiescent")
    assert context["non_terminal_workflows"] == [live["workflow_id"]]
    assert not (deployment.artifact() / "migrations").exists()


def test_cli_move_target_emits_structured_json(deployment, targets, capsys):
    old, new = targets
    cli = _module("odibi_anchor.cli")
    base = ["portfolio", "move-target", "--config", str(deployment.config), "--host", HOST,
            "--project", "alpha", "--from", str(old), "--to", str(new)]

    assert cli.main([*base, "--dry-run"]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["ok"] is True and preview["result"]["status"] == "ready"
    assert cli.main([*base[:-2], "--to", str(old.parent / "missing")]) == cli.EXIT_ACTION
    refused = json.loads(capsys.readouterr().out)
    assert refused["error"]["error_code"] == "target_migration_blocked"
    assert refused["error"]["context"]["classification"] == "destination_unsuitable"
    assert cli.main(["portfolio", "move-target", "--config", str(deployment.config), "--host", HOST]) == cli.EXIT_INPUT
    capsys.readouterr()
    assert cli.main(base) == 0
    assert json.loads(capsys.readouterr().out)["result"]["status"] == "completed"


def _crash_before_receipt(artifact: Path):
    return lambda point: point.operation == "os.open" and ".receipt.json." in point.path


def _snapshot_bytes(deployment: Deployment) -> dict[str, str]:
    return {"portfolio": _sha(deployment.config), **_tree(deployment.artifact())}


def test_rollback_after_a_new_epoch_started_refuses_without_writing_then_resume_converges(deployment, targets):
    old, new = targets
    artifact = deployment.artifact()
    injector = CrashInjector(scope=[deployment.root], crash_if=_crash_before_receipt(artifact))
    with injector, pytest.raises(InjectedCrash):
        deployment.move(source=old, destination=new)
    # The route is consistent after the portfolio write, so a launch starts the new epoch.
    assert deployment.prepare()["target_root"] == str(new)
    deployment.launch("alpha", new)
    # Live work on the new target must not hide that the rollback is impossible.
    workflow = _module("odibi_anchor.codebase._workflow")
    workflow.create_workflow(deployment.state / ".agent_memory.db", owner={
        "project_id": "alpha", "target_root": str(new), "artifact_root": str(artifact),
        "anchor_home": str(deployment.state.resolve()), "trust_domain": "work",
    }, request_id="live-on-new", plan={"schema_version": 1, "goal": "Live.", "risk": "low", "execution_mode": "read_only"})
    before = _snapshot_bytes(deployment)

    with pytest.raises(Exception) as refused:
        deployment.move(source=old, destination=new, rollback=True)

    code, context = _code(refused.value)
    assert (code, context["classification"], context["state"]) == ("target_migration_blocked", "state_mismatch", "portfolio_written")
    assert refused.value.next_operations[0]["arguments"]["resume"] is True
    assert _snapshot_bytes(deployment) == before
    assert deployment.move(source=old, destination=new, resume=True)["status"] == "completed"
    assert deployment.prepare()["target_root"] == str(new)


def test_rollback_is_refused_when_the_receipt_exists_but_its_journal_update_was_lost(deployment, targets):
    old, new = targets
    artifact = deployment.artifact()

    def after_receipt(point):
        receipts = list((artifact / "migrations").glob("tm_*.receipt.json"))
        return bool(receipts) and point.operation == "os.open" and "/migrations/.tm_" in point.path

    injector = CrashInjector(scope=[deployment.root], crash_if=after_receipt)
    with injector, pytest.raises(InjectedCrash):
        deployment.move(source=old, destination=new)
    before = _snapshot_bytes(deployment)

    with pytest.raises(Exception) as refused:
        deployment.move(source=old, destination=new, rollback=True)

    assert _code(refused.value)[1]["receipt_path"].endswith(".receipt.json")
    assert _snapshot_bytes(deployment) == before
    assert deployment.move(source=old, destination=new, resume=True)["status"] == "completed"


def test_receipt_chain_with_a_return_trip_names_the_latest_move_off_the_prior_target(deployment, targets, tmp_path):
    old, new = targets
    third = tmp_path / "third" / "alpha"
    third.mkdir(parents=True)
    deployment.move(source=old, destination=new)
    deployment.move(source=new, destination=old)
    last = deployment.move(source=old, destination=third)
    migration = _module("odibi_anchor._migration")

    receipt = migration.migration_receipt_for(deployment.artifact(), "alpha", str(old), str(third))

    assert receipt is not None and receipt["migration_id"] == last["migration_id"]
    assert migration.migration_receipt_for(deployment.artifact(), "alpha", str(old), str(tmp_path / "never")) is None


def test_destination_containing_anchor_state_is_refused(deployment, targets):
    old, _new = targets
    with pytest.raises(Exception) as refused:
        deployment.move(source=old, destination=deployment.root)
    code, context = _code(refused.value)
    assert (code, context["classification"]) == ("target_migration_blocked", "destination_unsuitable")
    assert any("contains the ANCHOR_HOME" in problem for problem in context["problems"])


def test_cli_bad_mapping_file_is_an_input_error(deployment, tmp_path, capsys):
    cli = _module("odibi_anchor.cli")
    mapping = tmp_path / "moves.json"
    mapping.write_text('{"moves": [{"project": "alpha"}]}', encoding="utf-8")

    code = cli.main(["portfolio", "move-target", "--config", str(deployment.config), "--host", HOST,
                     "--mapping", str(mapping)])

    assert code == cli.EXIT_INPUT
    assert json.loads(capsys.readouterr().out)["error"]["type"] == "input"


def test_original_owner_already_naming_the_destination_is_archived_not_mistaken_for_a_new_epoch(deployment, targets):
    old, new = targets
    owner_path = deployment.artifact() / "continuity" / "v1" / "OWNER.json"
    owner = json.loads(owner_path.read_bytes())
    owner_path.write_text(json.dumps({**owner, "target_root": str(new)}, sort_keys=True, separators=(",", ":")) + "\n",
                          encoding="utf-8")
    original = owner_path.read_bytes()
    deployment.write_portfolio({"alpha": str(new)})

    result = deployment.move(source=old, destination=new)

    assert result["status"] == "completed"
    archive = deployment.artifact() / "continuity" / "archive" / result["migration_id"]
    assert (archive / "OWNER.json").read_bytes() == original and not owner_path.exists()
    assert result["steps"]["continuity"]["new_epoch_started"] is False


def test_byte_identical_new_epoch_after_archive_still_counts_as_new(deployment, targets):
    old, new = targets
    artifact = deployment.artifact()
    owner_path = artifact / "continuity" / "v1" / "OWNER.json"
    owner = json.loads(owner_path.read_bytes())
    owner_path.write_text(json.dumps({**owner, "target_root": str(new)}, sort_keys=True, separators=(",", ":")) + "\n",
                          encoding="utf-8")
    deployment.write_portfolio({"alpha": str(new)})
    injector = CrashInjector(scope=[deployment.root], crash_if=_crash_before_receipt(artifact))
    with injector, pytest.raises(InjectedCrash):
        deployment.move(source=old, destination=new)
    deployment.launch("alpha", new)  # recreates v1 with the same five owner fields
    _module("odibi_anchor.codebase._workflow").create_workflow(deployment.state / ".agent_memory.db", owner={
        "project_id": "alpha", "target_root": str(new), "artifact_root": str(artifact),
        "anchor_home": str(deployment.state.resolve()), "trust_domain": "work",
    }, request_id="live-on-new", plan={"schema_version": 1, "goal": "Live.", "risk": "low", "execution_mode": "read_only"})

    with pytest.raises(Exception) as refused:
        deployment.move(source=old, destination=new, rollback=True)

    assert "continuity" in _code(refused.value)[1]  # the new-epoch refusal, not a generic mismatch
    assert deployment.move(source=old, destination=new, resume=True)["status"] == "completed"
