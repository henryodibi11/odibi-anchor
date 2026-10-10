"""End-to-end replay of ``portfolio move-target`` on a fake Databricks runtime (#30).

Covers the full journaled move across computes, a fresh compute that restores the
pending snapshot before the portfolio write, quiescence, and a crash injected before
every local and fake-Databricks mutation point. After each crash every store is fully
old or fully new (or the journal names the resume step), nothing is deleted, the next
bootstrap either succeeds or diagnoses ``migration_pending``, and resume or rollback
converges to a bootable project.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import os
from pathlib import Path
from typing import Any

import pytest

from tests.fixtures.fake_databricks import FakeDatabricks
from tests.fixtures.fake_databricks.crash import CrashInjector, InjectedCrash, record_mutations
from tests.integration.conftest import ReplayHarness

HOST = "databricks"


def _sha(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def _recovery(exc: BaseException) -> tuple[str | None, dict[str, Any]]:
    return getattr(exc, "error_code", None), dict(getattr(exc, "context", {}) or {})


def _migration() -> Any:
    return importlib.import_module("odibi_anchor._migration")


def _move(replay: ReplayHarness, source: Path, destination: Path, **kwargs: Any) -> dict[str, Any]:
    replay.new_process()
    return _migration().move_target(
        config_path=replay.config, host_id=HOST, project_id="alpha",
        from_target=str(source), to_target=str(destination), **kwargs,
    )


def _boot(replay: ReplayHarness) -> dict[str, Any]:
    replay.new_process()
    return replay.bootstrap("alpha")


class Moved:
    """One launched project with its observed pre-migration bytes."""

    def __init__(self, replay: ReplayHarness, start: str = "aligned") -> None:
        self.replay = replay
        self.start = start
        self.old = replay.target("alpha")
        self.new = replay.target("projects/alpha")
        created = replay.create_project("alpha", self.old)
        self.artifact = Path(created["startup_packet"]["artifact_root"])
        if start == "portfolio_already_moved":
            # The #30 incident: the portfolio was edited by hand; the descriptor still names old.
            document = importlib.import_module("odibi_anchor.portfolio").load_portfolio_document(replay.config)
            replay.write_portfolio({"alpha": str(self.new)}, expected_sha256=document["sha256"])
        self.owner = self.artifact / "continuity" / "v1" / "OWNER.json"
        self.pre = {
            "portfolio": replay.config.read_bytes(),
            "descriptor": (self.artifact / "PROJECT.md").read_bytes(),
            "owner": self.owner.read_bytes(),
        }

    def journals(self) -> list[dict[str, Any]]:
        root = self.artifact / "migrations"
        paths = sorted(root.glob("tm_*.json")) if root.is_dir() else []
        return [json.loads(path.read_bytes()) for path in paths if not path.name.endswith(".receipt.json")]

    def archives(self) -> list[Path]:
        root = self.artifact / "continuity" / "archive"
        return sorted(root.iterdir()) if root.is_dir() else []

    def store_states(self, expected_after: dict[str, str]) -> dict[str, str]:
        """Classify each store as old or new; anything else fails the invariant."""
        pre_sha = {name: hashlib.sha256(data).hexdigest() for name, data in self.pre.items()}
        states = {}
        for name, path in (("portfolio", self.replay.config), ("descriptor", self.artifact / "PROJECT.md")):
            current = _sha(path)
            assert current in {pre_sha[name], expected_after[name]}, f"{name} is neither old nor new"
            states[name] = "new" if current == expected_after[name] else "old"
        archives = self.archives()
        if self.owner.is_file() and not archives:
            assert _sha(self.owner) == pre_sha["owner"]
            states["continuity"] = "old"
        else:
            assert not self.owner.exists() and len(archives) == 1, "continuity is split"
            assert _sha(archives[0] / "OWNER.json") == pre_sha["owner"]
            states["continuity"] = "new"
        return states

    def assert_nothing_deleted(self) -> None:
        pre_sha = {name: hashlib.sha256(data).hexdigest() for name, data in self.pre.items()}
        backups = {path.name: _sha(path) for path in (self.artifact / "migrations").rglob("*.pre")} \
            if (self.artifact / "migrations").is_dir() else {}
        assert pre_sha["portfolio"] in {_sha(self.replay.config), backups.get("portfolio.toml.pre")}
        assert pre_sha["descriptor"] in {_sha(self.artifact / "PROJECT.md"), backups.get("PROJECT.md.pre")}
        assert pre_sha["owner"] in {_sha(self.owner), *(_sha(path / "OWNER.json") for path in self.archives())}


def test_move_target_replay_across_computes(replay, databricks):
    moved = Moved(replay)
    workflow = importlib.import_module("odibi_anchor.codebase._workflow")
    environment = replay.bootstrap("alpha")["preparation"]["environment"]
    database = Path(environment["ANCHOR_MEMORY_DB"])
    owner = {"project_id": "alpha", "target_root": str(moved.old), "artifact_root": str(moved.artifact),
             "anchor_home": str(Path(environment["ANCHOR_HOME"]).resolve()), "trust_domain": "work"}
    history = workflow.create_workflow(database, owner=owner, request_id="history", plan={
        "schema_version": 1, "goal": "Historical work.", "risk": "low", "execution_mode": "read_only",
    })
    workflow.transition_workflow(database, owner=owner, workflow_id=history["workflow_id"], expected_generation=0,
                                 request_id="history-cancel", operation="cancel", payload={"reason": "done"})

    preview = _move(replay, moved.old, moved.new, dry_run=True)
    assert preview["status"] == "ready" and moved.journals() == []
    result = _move(replay, moved.old, moved.new)
    assert result["status"] == "completed"
    assert {name: result["steps"][name]["status"] for name in ("pre_snapshot", "pending_snapshot", "post_snapshot")} == {
        "pre_snapshot": "done", "pending_snapshot": "done", "post_snapshot": "done",
    }
    assert _move(replay, moved.old, moved.new)["status"] == "already_migrated"

    replay.new_compute()
    rebooted = _boot(replay)

    packet = rebooted["startup_packet"]
    assert packet["restore"]["snapshot_id"] == result["steps"]["post_snapshot"]["snapshot_id"]
    assert packet["target_root"] == str(moved.new)
    artifact = Path(packet["artifact_root"])
    assert artifact.name == "alpha" and artifact != moved.artifact  # a new compute root, same project
    new_owner = json.loads((artifact / "continuity" / "v1" / "OWNER.json").read_bytes())
    assert new_owner["target_root"] == str(moved.new)
    current = {**owner, "target_root": str(moved.new), "artifact_root": str(artifact),
               "anchor_home": str(Path(rebooted["preparation"]["environment"]["ANCHOR_HOME"]).resolve())}
    with pytest.raises(workflow.WorkflowError) as caught:
        workflow.read_workflow(rebooted["preparation"]["environment"]["ANCHOR_MEMORY_DB"], owner=current,
                               workflow_id=history["workflow_id"])
    code, context = _recovery(caught.value)
    assert (code, context["prior_target"], context["migration_id"]) == (
        "workflow_bound_to_prior_target", str(moved.old), result["migration_id"],
    )


def test_open_task_window_blocks_the_move_with_its_id(replay):
    moved = Moved(replay)
    anchor = _boot(replay)["anchor"]
    task = replay.start_analysis_task(anchor)

    with pytest.raises(Exception) as refused:
        _move(replay, moved.old, moved.new)

    code, context = _recovery(refused.value)
    assert (code, context["classification"]) == ("target_migration_blocked", "not_quiescent")
    assert context["open_task_windows"] == [task["task_window_id"]]
    assert moved.journals() == []


def _before_portfolio_write(replay: ReplayHarness):
    parent = str(replay.config.parent.resolve())
    return lambda point: point.operation == "os.open" and point.path.startswith(parent + os.sep + ".anchor.toml.")


@pytest.mark.parametrize("recovery", ["resume", "rollback"])
def test_fresh_compute_restoring_before_portfolio_write_is_migration_pending(replay, databricks, recovery):
    moved = Moved(replay)
    crash = CrashInjector(scope=[replay.root, databricks.root], crash_if=_before_portfolio_write(replay))
    with crash, pytest.raises(InjectedCrash):
        _move(replay, moved.old, moved.new)
    replay.new_compute()

    with pytest.raises(Exception) as caught:
        _boot(replay)

    code, context = _recovery(caught.value)
    assert (code, context["classification"]) == ("route_target_conflict", "migration_pending")
    assert (context["requested_target"], context["descriptor_target"]) == (str(moved.old), str(moved.new))
    assert context["migration"]["state"] == "portfolio_pending"
    assert Path(context["config_path"]) == replay.config
    copy_ready = [operation["copy_ready"] for operation in caught.value.next_operations]
    assert [command.split()[-1] for command in copy_ready] == ["--resume", "--rollback"]

    result = _move(replay, moved.old, moved.new, **{recovery: True})

    expected = moved.new if recovery == "resume" else moved.old
    assert result["status"] == ("completed" if recovery == "resume" else "rolled_back")
    if recovery == "rollback":
        # The epoch was archived on the first compute; it stays preserved there.
        assert result["steps"]["continuity"]["status"] == "left_archived"
    assert _boot(replay)["startup_packet"]["target_root"] == str(expected)
    replay.new_compute()
    assert _boot(replay)["startup_packet"]["target_root"] == str(expected)


def _deploy(root: Path, monkeypatch: pytest.MonkeyPatch, start: str) -> tuple[ReplayHarness, FakeDatabricks, Moved]:
    FakeDatabricks.new_process()
    databricks = FakeDatabricks(root / "databricks").install(monkeypatch)
    replay = ReplayHarness(root / "deployment", databricks)
    return replay, databricks, Moved(replay, start)


def _boot_outcome(replay: ReplayHarness) -> tuple[str, dict[str, Any]]:
    """Bootstrap and name the outcome: the bound target, or the route-conflict classification."""
    try:
        booted = _boot(replay)
    except Exception as exc:
        code, context = _recovery(exc)
        assert code == "route_target_conflict", repr(exc)
        return context["classification"], context
    return "booted", {"target_root": booted["startup_packet"]["target_root"]}


@pytest.mark.parametrize("start", ["aligned", "portfolio_already_moved"])
@pytest.mark.parametrize("recovery", ["resume", "rollback"])
def test_crash_before_every_mutation_point_converges(tmp_path, monkeypatch, recovery, start):
    replay, databricks, moved = _deploy(tmp_path / "baseline", monkeypatch, start)
    _result, points = record_mutations(
        lambda: _move(replay, moved.old, moved.new), scope=[replay.root, databricks.root],
    )
    kinds = {point.kind for point in points}
    assert kinds == {"filesystem", "remote"} and len(points) >= 30
    diagnoses: set[str] = set()
    # Before the move, an aligned route boots on old; the incident state is ambiguous.
    initial_outcome = "booted_old" if start == "aligned" else "ambiguous"

    for point in points:
        replay, databricks, moved = _deploy(tmp_path / f"crash-{point.index}", monkeypatch, start)
        preview = _move(replay, moved.old, moved.new, dry_run=True)
        expected_after = {"portfolio": preview["expected"]["portfolio_sha256"],
                          "descriptor": preview["expected"]["descriptor_sha256"]}
        initial = moved.store_states(expected_after)
        injector = CrashInjector(scope=[replay.root, databricks.root], crash_at=point.index)
        with injector, pytest.raises(InjectedCrash):
            _move(replay, moved.old, moved.new)
        assert injector.crashed is not None
        assert (injector.crashed.kind, injector.crashed.operation) == (point.kind, point.operation)
        label = f"{start}: crash before {point.index} {point.operation}"

        states = moved.store_states(expected_after)
        moved.assert_nothing_deleted()
        journals = moved.journals()
        all_new = set(states.values()) == {"new"}
        if states != initial and not all_new:
            assert journals and journals[0]["state"] in {"planned", "portfolio_pending", "portfolio_written"}, label

        if recovery == "resume":
            # The next bootstrap is a consistent route or an exact diagnosis, never a bare error.
            outcome, context = _boot_outcome(replay)
            if outcome == "booted":
                assert states == initial or all_new, label
                target = moved.new if all_new else moved.old
                assert context["target_root"] == str(target), label
                outcome = "booted_new" if all_new else "booted_old"
            elif journals and states != initial:
                assert outcome == "migration_pending", label
                assert context["migration"]["migration_id"] == journals[0]["migration_id"], label
            else:
                assert outcome in {"migration_pending", initial_outcome}, label
            diagnoses.add(outcome)
            if journals:
                result = _move(replay, moved.old, moved.new, resume=True)
            else:
                result = _move(replay, moved.old, moved.new)
            assert result["status"] == "completed", label
            final = "new"
        else:
            receipt_exists = bool(list((moved.artifact / "migrations").glob("tm_*.receipt.json"))) \
                if (moved.artifact / "migrations").is_dir() else False
            if not journals:
                final = "initial"
            elif receipt_exists:
                with pytest.raises(Exception) as refused:
                    _move(replay, moved.old, moved.new, rollback=True)
                assert _recovery(refused.value)[1]["classification"] == "state_mismatch", label
                assert _move(replay, moved.old, moved.new, resume=True)["status"] == "completed", label
                final = "new"
            else:
                assert _move(replay, moved.old, moved.new, rollback=True)["status"] == "rolled_back", label
                assert moved.store_states(expected_after) == initial, label
                final = "initial"
        moved.assert_nothing_deleted()
        outcome, context = _boot_outcome(replay)
        if final == "new":
            assert (outcome, context.get("target_root")) == ("booted", str(moved.new)), label
        elif start == "aligned":
            assert (outcome, context.get("target_root")) == ("booted", str(moved.old)), label
        else:
            assert outcome == "ambiguous", label
    if recovery == "resume":
        expected = {"booted_old", "booted_new", "migration_pending"} if start == "aligned" \
            else {"ambiguous", "booted_new", "migration_pending"}
        assert diagnoses == expected
