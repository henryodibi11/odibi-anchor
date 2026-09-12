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
