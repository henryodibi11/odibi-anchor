"""Dispatcher terminalization binds gate, learning, memory, and truth boundaries."""

from types import SimpleNamespace
from typing import Any

import pytest

from odibi_anchor._dispatcher._post_dispatch import _persist_terminal_task_if_ready
from odibi_anchor.codebase._task_execution import inspect_terminal_records


def state(tmp_path):
    return SimpleNamespace(
        task_verification_epoch=0, task_repository_baseline=None,
        task_repository_write_fingerprints={}, evidence_ledger=[], managed_artifact_ledger=[],
        active_project="project:test", linked_problem="PRB-2026-0017",
        linked_spec="SPEC", linked_work_item="WI-2026-0011",
        task_window_id="ltw-terminal", session_id="session-terminal",
        terminal_reason=None, artifact_root=str(tmp_path),
    )


def test_read_only_assessment_persists_truthful_no_change_terminal_record(tmp_path, monkeypatch):
    import odibi_anchor._dispatcher._boot as boot

    db = tmp_path / "memory.db"
    monkeypatch.setitem(boot._ENV, "memory_db", str(db))
    current = state(tmp_path)
    current.active_task_profile = SimpleNamespace(execution_mode="read_only")
    result = {"assessment": {
        "assessment_id": "las-read", "outcome": "nothing_reusable_learned",
        "observation_ids": [], "actor_kind": "agent",
    }}
    _persist_terminal_task_if_ready(
        result, action="learning", args=("assess",),
        session_timings=[{
            "action": "learning", "learning_command": "assess", "elapsed_ms": 1.5,
        }],
        session_files_changed=set(), session_state=current,
    )
    record = result["terminal_task_record"]["record"]
    assert record["terminal"] == {
        "status": "completed", "basis": "no_change_assessment_committed",
        "reason": None, "ended_at": record["ended_at"],
    }
    assert record["repository"]["changed_paths"] == []
    assert "gate check was not observed" in record["coverage"]["unavailable"]


def test_completed_assessment_persists_one_restart_readable_record(tmp_path, monkeypatch):
    import odibi_anchor._dispatcher._boot as boot

    db = tmp_path / "memory.db"
    monkeypatch.setitem(boot._ENV, "memory_db", str(db))
    result = {"assessment": {
        "assessment_id": "las-1", "outcome": "nothing_reusable_learned",
        "observation_ids": [], "actor_kind": "agent",
    }}
    timings = [
        {"action": "preflight", "passed": True},
        {"action": "test", "passed": True, "exit_code": 0},
        {"action": "review", "passed": True},
        {"action": "gate", "passed": True},
        {"action": "learning", "learning_command": "assess"},
    ]
    _persist_terminal_task_if_ready(
        result, action="learning", args=("assess",), session_timings=timings,
        session_files_changed={"src/a.py"}, session_state=state(tmp_path),
    )
    assert result["terminal_task_record"]["record"]["terminal"]["status"] == "completed"
    retained = inspect_terminal_records(db, task_window_id="ltw-terminal")["records"][0]
    assert retained["record"]["identities"]["work_item_id"] == "WI-2026-0011"
    assert "task-start repository revision was not captured" in retained["record"]["coverage"]["unavailable"]

    retry = {"assessment": result["assessment"]}
    _persist_terminal_task_if_ready(
        retry, action="learning", args=("assess",), session_timings=timings,
        session_files_changed={"src/a.py"}, session_state=state(tmp_path),
    )
    assert retry["terminal_task_record"]["record_sha256"] == retained["record_sha256"]


def test_terminal_closure_snapshots_configured_durable_state(tmp_path, monkeypatch):
    import odibi_anchor._dispatcher._boot as boot

    db = tmp_path / "memory.db"
    durable = tmp_path / "durable"
    durable.mkdir()
    (tmp_path / "workspace" / "projects").mkdir(parents=True)
    monkeypatch.setitem(boot._ENV, "memory_db", str(db))
    monkeypatch.setitem(boot._ENV, "durable_root", str(durable))
    monkeypatch.setitem(boot._ENV, "authority_id", "work")
    monkeypatch.setitem(boot._ENV, "trust_domain", "work")
    monkeypatch.setitem(boot._ENV, "runtime_paths", SimpleNamespace(anchor_home=tmp_path))
    monkeypatch.setitem(boot._ENV, "retention_days", 7)
    monkeypatch.setitem(boot._ENV, "retention_minimum_snapshots", 3)
    result = {"assessment": {
        "assessment_id": "las-durable", "outcome": "nothing_reusable_learned",
        "observation_ids": [], "actor_kind": "agent",
    }}

    _persist_terminal_task_if_ready(
        result, action="learning", args=("assess",),
        session_timings=[{"action": "gate", "passed": True}],
        session_files_changed={"src/a.py"}, session_state=state(tmp_path),
    )

    assert result["durable_state"]["action"] == "created"
    assert result["durable_state"]["retention"] == {
        "status": "applied", "days": 7, "minimum_snapshots": 3,
        "removed_snapshots": 0, "removed_blobs": 0, "retained_snapshots": 1,
    }
    assert result["durable_state"]["manifest"]["format"] == "odibi-anchor-durable-snapshot-v2"
    assert result["accepted_task_closure"]["status"] == "unavailable"
    assert list((durable / "work" / "snapshots").glob("*.manifest.json"))


def test_durable_checkpoint_ignores_environment_redirection_after_boot(tmp_path, monkeypatch):
    import sqlite3

    import odibi_anchor._dispatcher._boot as boot
    from odibi_anchor._dispatcher._post_dispatch import _snapshot_durable_state

    database = tmp_path / "memory.db"
    bound = tmp_path / "bound-durable"
    redirected = tmp_path / "redirected-durable"
    bound.mkdir()
    redirected.mkdir()
    (tmp_path / "workspace" / "projects").mkdir(parents=True)
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE evidence(value TEXT)")
    monkeypatch.setitem(boot._ENV, "durable_root", str(bound))
    monkeypatch.setitem(boot._ENV, "authority_id", "bound-authority")
    monkeypatch.setitem(boot._ENV, "trust_domain", "work")
    monkeypatch.setitem(boot._ENV, "is_databricks", False)
    monkeypatch.setitem(boot._ENV, "runtime_paths", SimpleNamespace(anchor_home=tmp_path))
    monkeypatch.setenv("ANCHOR_DURABLE_ROOT", str(redirected))
    monkeypatch.setenv("ANCHOR_AUTHORITY_ID", "redirected-authority")
    result = {}

    _snapshot_durable_state(result, memory_db=str(database))

    assert result["durable_state"]["authority"]["authority_id"] == "bound-authority"
    assert list((bound / "bound-authority" / "snapshots").glob("*.manifest.json"))
    assert not any(redirected.iterdir())


def test_nonterminal_checkpoint_defers_configured_retention_scan(tmp_path, monkeypatch):
    import odibi_anchor._dispatcher._boot as boot
    from odibi_anchor._dispatcher._post_dispatch import _snapshot_durable_state

    observed = []
    monkeypatch.setitem(boot._ENV, "durable_root", "/durable")
    monkeypatch.setitem(boot._ENV, "authority_id", "work")
    monkeypatch.setitem(boot._ENV, "trust_domain", "work")
    monkeypatch.setitem(boot._ENV, "is_databricks", True)
    monkeypatch.setitem(boot._ENV, "runtime_paths", SimpleNamespace(anchor_home=tmp_path))
    monkeypatch.setitem(boot._ENV, "retention_days", 1)
    monkeypatch.setitem(boot._ENV, "retention_minimum_snapshots", 3)
    monkeypatch.setattr(
        "odibi_anchor.durability.snapshot_state",
        lambda **kwargs: observed.append(kwargs) or {"action": "created"},
    )

    result = {}
    _snapshot_durable_state(result, memory_db=str(tmp_path / "memory.db"))

    assert observed[0]["retention_days"] is None
    assert observed[0]["minimum_snapshots"] is None


def test_latest_failed_gate_prevents_false_completion(tmp_path, monkeypatch):
    import odibi_anchor._dispatcher._boot as boot

    db = tmp_path / "memory.db"
    monkeypatch.setitem(boot._ENV, "memory_db", str(db))
    result = {"assessment": {"assessment_id": "las-1", "outcome": "nothing_reusable_learned"}}
    _persist_terminal_task_if_ready(
        result, action="learning", args=("assess",),
        session_timings=[
            {"action": "gate", "passed": True},
            {"action": "gate", "passed": False},
            {"action": "learning", "learning_command": "assess"},
        ],
        session_files_changed={"src/a.py"}, session_state=state(tmp_path),
    )
    assert "terminal_task_record" not in result
    assert inspect_terminal_records(db)["schema_status"] == "uninitialized"


@pytest.mark.parametrize("terminal_status", ["blocked", "failed"])
def test_safe_stop_persists_terminal_status_and_unavailable_evidence(
    tmp_path, monkeypatch, terminal_status,
):
    import odibi_anchor._dispatcher._boot as boot

    db = tmp_path / "memory.db"
    monkeypatch.setitem(boot._ENV, "memory_db", str(db))
    current = state(tmp_path)
    current.terminal_status = terminal_status
    result: dict[str, Any] = {
        "samples": {"unavailable_evidence": ["external CI was not observed"]},
    }
    _persist_terminal_task_if_ready(
        result, action="learning", args=("safe_stop",), session_timings=[],
        session_files_changed=set(), session_state=current,
    )
    record = result["terminal_task_record"]["record"]
    assert record["terminal"]["status"] == terminal_status
    assert "external CI was not observed" in record["coverage"]["unavailable"]


def _durable_terminal_environment(tmp_path, monkeypatch):
    import odibi_anchor._dispatcher._boot as boot

    durable = tmp_path / "durable"
    durable.mkdir()
    (tmp_path / "workspace" / "projects").mkdir(parents=True)
    monkeypatch.setitem(boot._ENV, "memory_db", str(tmp_path / "memory.db"))
    monkeypatch.setitem(boot._ENV, "durable_root", str(durable))
    monkeypatch.setitem(boot._ENV, "authority_id", "work")
    monkeypatch.setitem(boot._ENV, "trust_domain", "work")
    monkeypatch.setitem(boot._ENV, "is_databricks", False)
    monkeypatch.setitem(boot._ENV, "runtime_paths", SimpleNamespace(anchor_home=tmp_path))
    monkeypatch.setitem(boot._ENV, "retention_days", None)
    monkeypatch.setitem(boot._ENV, "retention_minimum_snapshots", None)
    return durable


def _held_staging(monkeypatch, tmp_path):
    import errno
    import shutil
    import tempfile
    from pathlib import Path

    from odibi_anchor import durability

    # Retained staging must stay inside the test directory, not the system temp.
    (tmp_path / "staging-temp").mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "staging-temp"))
    real_rmtree = shutil.rmtree

    def rmtree(path, *args, **kwargs):
        if Path(path).name.startswith("odibi-anchor-snapshot-"):
            error = PermissionError(errno.EACCES, "held", str(Path(path) / "artifacts.tar"))
            error.winerror = 32  # type: ignore[attr-defined]
            raise error
        real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(durability.shutil, "rmtree", rmtree)
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        assert len(sleeps) <= 10, "staging removal retry is unbounded"

    monkeypatch.setattr(durability.time, "sleep", sleep)


def _read_only_assess(tmp_path):
    current = state(tmp_path)
    current.active_task_profile = SimpleNamespace(execution_mode="read_only")
    result = {"assessment": {
        "assessment_id": "las-held", "outcome": "observations_recorded",
        "observation_ids": ["lrn_synthetic"], "actor_kind": "agent",
    }}
    _persist_terminal_task_if_ready(
        result, action="learning", args=("assess",),
        session_timings=[{"action": "learning", "learning_command": "assess"}],
        session_files_changed=set(), session_state=current,
    )
    return result


def test_read_only_closure_reports_durable_snapshot_when_only_staging_removal_is_held(
    tmp_path, monkeypatch,
):
    durable = _durable_terminal_environment(tmp_path, monkeypatch)
    _held_staging(monkeypatch, tmp_path)

    result = _read_only_assess(tmp_path)

    assert result["terminal_task_record"]["record"]["terminal"]["status"] == "completed"
    assert result["durable_state"]["action"] == "created"
    assert result["durable_state"]["staging_cleanup"]["status"] == "retained"
    assert list((durable / "work" / "snapshots").glob("*.manifest.json"))


def test_snapshot_refusal_after_closure_says_committed_writes_were_not_rolled_back(
    tmp_path, monkeypatch,
):
    from odibi_anchor import durability

    _durable_terminal_environment(tmp_path, monkeypatch)
    _held_staging(monkeypatch, tmp_path)

    def fail_manifest(source, destination):
        raise OSError("simulated manifest publication failure")

    monkeypatch.setattr(durability, "_publish_file_exclusive", fail_manifest)

    with pytest.raises(RuntimeError) as refusal:
        _read_only_assess(tmp_path)

    message = str(refusal.value)
    assert message.startswith(
        "BLOCKED: active state could not be durably snapshotted: OSError: simulated manifest publication failure"
    )
    assert "snapshot staging directory was retained" in message
    assert "committed and were not rolled back; read state back before retrying" in message
    records = inspect_terminal_records(tmp_path / "memory.db", task_window_id="ltw-terminal")["records"]
    assert [record["record"]["terminal"]["status"] for record in records] == ["completed"]
