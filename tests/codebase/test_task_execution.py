"""Durability and truth-boundary contracts for terminal task records."""

import json
import sqlite3
import subprocess
from dataclasses import replace
from types import SimpleNamespace

import pytest

from odibi_anchor._forensic_replay.journal import redact_payload
from odibi_anchor.codebase._task_execution import (
    FORMAT,
    UNOBSERVED,
    V1_SCHEMA_SHA256,
    _action_fact,
    build_terminal_projection,
    initialize_schema,
    inspect_terminal_records,
    persist_terminal_record,
)


def record(task: str = "ltw-1") -> dict:
    return {
        "format": FORMAT,
        "identities": {
            "task_window_id": task,
            "session_id": "session-1",
            "project_id": "public-project",
            "problem_id": "PRB-2026-0017",
            "spec_id": "SPEC",
            "work_item_id": "WI-2026-0011",
        },
        "repository": {"start_revision": "a" * 40, "end_revision": "b" * 40},
        "started_at": "2026-09-01T00:00:00Z",
        "ended_at": "2026-09-01T00:01:00Z",
        "terminal": {"status": "completed"},
        "coverage": {
            "unobserved": list(UNOBSERVED),
            "unavailable": ["external CI was not observed"],
            "replay_claim": "none",
        },
        "evidence": [{"token": "secret-value"}],
    }


def test_absent_inspection_is_read_only(tmp_path):
    path = tmp_path / "nested" / "memory.db"
    assert inspect_terminal_records(path) == {"schema_status": "uninitialized", "records": []}
    assert not path.exists() and not path.parent.exists()


@pytest.mark.parametrize("drift", [None, "table", "index"])
def test_v1_terminal_schema_migrates_only_when_exact(tmp_path, drift):
    path = tmp_path / "memory.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE anchor_schema_versions (domain TEXT PRIMARY KEY, version INTEGER NOT NULL CHECK(version>0), schema_sha256 TEXT NOT NULL CHECK(length(schema_sha256)=64), applied_at TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE terminal_task_records (task_window_id TEXT PRIMARY KEY, record_id TEXT NOT NULL UNIQUE, project_id TEXT, problem_id TEXT, spec_id TEXT, work_item_id TEXT, session_id TEXT NOT NULL, terminal_status TEXT NOT NULL CHECK(terminal_status IN ('completed','blocked','failed')), started_at TEXT NOT NULL, ended_at TEXT NOT NULL, start_revision TEXT, end_revision TEXT, record_json TEXT NOT NULL, record_sha256 TEXT NOT NULL CHECK(length(record_sha256)=64), created_at TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE INDEX idx_terminal_task_project ON terminal_task_records(project_id,ended_at,task_window_id)"
        )
        connection.execute(
            "INSERT INTO anchor_schema_versions VALUES('task_execution',1,?,'2026-09-01T00:00:00Z')",
            (V1_SCHEMA_SHA256,),
        )
        if drift == "table":
            connection.execute("ALTER TABLE terminal_task_records ADD COLUMN drift TEXT")
        elif drift == "index":
            connection.execute("DROP INDEX idx_terminal_task_project")
            connection.execute(
                "CREATE INDEX idx_terminal_task_project ON terminal_task_records(project_id)"
            )
    if drift:
        with pytest.raises(RuntimeError, match="schema checksum mismatch"):
            persist_terminal_record(path, record())
        assert not sorted(path.parent.glob(path.name + ".pre-task-execution-v2.*.bak"))
        return
    before = inspect_terminal_records(path)
    assert before == {"schema_status": "legacy_read_only", "records": []}
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT version FROM anchor_schema_versions WHERE domain='task_execution'"
        ).fetchone()[0] == 1
    persisted = persist_terminal_record(path, record())
    assert persisted["record_id"].startswith("ttr_")
    assert len(sorted(path.parent.glob(path.name + ".pre-task-execution-v2.*.bak"))) == 1
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT version FROM anchor_schema_versions WHERE domain='task_execution'"
        ).fetchone()[0] == 2
        assert connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='trigger' AND tbl_name='terminal_task_records'"
        ).fetchone()[0] == 2


def test_v1_terminal_migration_binds_legacy_payload_record_id_and_retains_backup(tmp_path):
    path = tmp_path / "memory.db"
    persisted = persist_terminal_record(path, record())
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TRIGGER terminal_task_records_no_update")
        connection.execute("DROP TRIGGER terminal_task_records_no_delete")
        raw, = connection.execute("SELECT record_json FROM terminal_task_records").fetchone()
        payload = json.loads(raw)
        payload["record_id"] = None
        legacy = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        digest = __import__("hashlib").sha256(legacy.encode()).hexdigest()
        connection.execute(
            "UPDATE terminal_task_records SET record_json=?,record_sha256=?", (legacy, digest),
        )
        connection.execute(
            "UPDATE anchor_schema_versions SET version=1,schema_sha256=? WHERE domain='task_execution'",
            (V1_SCHEMA_SHA256,),
        )
    initialize_schema(path)
    retained = inspect_terminal_records(path)["records"][0]
    assert retained["record"]["record_id"] == persisted["record_id"]
    backup, = sorted(path.parent.glob(path.name + ".pre-task-execution-v2.*.bak"))
    with sqlite3.connect(backup) as connection:
        legacy_payload, = connection.execute("SELECT record_json FROM terminal_task_records").fetchone()
    assert json.loads(legacy_payload)["record_id"] is None


def test_terminal_record_persists_across_connections_and_is_redacted(tmp_path):
    path = tmp_path / "memory.db"
    first = persist_terminal_record(path, record())
    assert first == persist_terminal_record(path, record())
    inspected = inspect_terminal_records(path, task_window_id="ltw-1")
    assert inspected["records"][0]["valid"] is True
    retained = inspected["records"][0]["record"]
    assert retained["record_id"] == first["record_id"]
    assert retained["evidence"] == [{"token": "[REDACTED]"}]
    assert retained["coverage"]["replay_claim"] == "none"
    assert retained["identities"]["work_item_id"] == "WI-2026-0011"


def test_terminal_record_rejects_conflict_and_missing_truth_boundaries(tmp_path):
    path = tmp_path / "memory.db"
    persist_terminal_record(path, record())
    changed = record()
    changed["terminal"]["status"] = "failed"
    with pytest.raises(ValueError, match="idempotency conflict"):
        persist_terminal_record(path, changed)
    invalid = record("ltw-2")
    invalid["coverage"]["unobserved"] = []
    with pytest.raises(ValueError, match="unobserved boundaries"):
        persist_terminal_record(tmp_path / "other.db", invalid)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda value: value.update(format="other"), "format"),
        (lambda value: value["coverage"].update(replay_claim="complete"), "replay_claim"),
    ],
)
def test_terminal_record_rejects_false_contract_claims(tmp_path, mutation, message):
    value = record()
    mutation(value)
    with pytest.raises(ValueError, match=message):
        persist_terminal_record(tmp_path / "memory.db", value)


def test_terminal_rows_are_append_only_and_retry_conflicts_fail(tmp_path):
    path = tmp_path / "memory.db"
    persist_terminal_record(path, record())
    with sqlite3.connect(path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("UPDATE terminal_task_records SET project_id='tampered'")
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("DELETE FROM terminal_task_records")
    changed = record()
    changed["coverage"]["unavailable"].append("new fact")
    with pytest.raises(ValueError, match="idempotency conflict"):
        persist_terminal_record(path, changed)


@pytest.mark.parametrize("tamper", ["indexed", "record_id", "canonical", "payload"])
def test_terminal_inspection_rejects_storage_tampering(tmp_path, tamper):
    path = tmp_path / "memory.db"
    persist_terminal_record(path, record())
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TRIGGER terminal_task_records_no_update")
        if tamper == "indexed":
            connection.execute("UPDATE terminal_task_records SET project_id='private:other'")
        elif tamper == "record_id":
            connection.execute("UPDATE terminal_task_records SET record_id='ttr_tampered'")
        else:
            raw, = connection.execute("SELECT record_json FROM terminal_task_records").fetchone()
            payload = json.loads(raw)
            if tamper == "payload":
                payload["terminal"]["status"] = "failed"
                rewritten = json.dumps(payload, sort_keys=True, separators=(",", ":"))
            else:
                rewritten = json.dumps(payload, sort_keys=True, indent=2)
            digest = __import__("hashlib").sha256(rewritten.encode()).hexdigest()
            connection.execute(
                "UPDATE terminal_task_records SET record_json=?,record_sha256=?",
                (rewritten, digest),
            )
        connection.execute(
            "CREATE TRIGGER terminal_task_records_no_update BEFORE UPDATE ON terminal_task_records "
            "BEGIN SELECT RAISE(ABORT,'terminal task records are immutable'); END"
        )
    match = (
        "indexed column mismatch" if tamper in {"indexed", "record_id", "payload"}
        else "not canonical JSON"
    )
    with pytest.raises(RuntimeError, match=match):
        inspect_terminal_records(path)


def test_action_projection_bounds_nested_result_without_claiming_replay():
    fact = _action_fact({
        "action": "gate",
        "passed": True,
        "elapsed_ms": 12.5,
        "result": {
            "kind": "workflow_gate_context",
            "summary": "passed",
            "metrics": {f"control-{index}": list(range(100)) for index in range(100)},
        },
    })
    assert fact == {
        "action": "gate",
        "passed": True,
        "elapsed_ms": 12.5,
        "result_kind": "workflow_gate_context",
        "result_summary": "passed",
    }


def test_terminal_projection_bounds_long_action_and_evidence_ledgers(tmp_path):
    state = SimpleNamespace(
        task_verification_epoch=0,
        task_repository_baseline=None,
        task_repository_write_fingerprints={},
        evidence_ledger=[{"index": index} for index in range(40)],
        managed_artifact_ledger=[],
        artifact_root=str(tmp_path),
        task_window_id="long-task",
        active_project="project:test",
        linked_problem=None,
        linked_spec=None,
        linked_work_item=None,
        session_id="long-session",
        terminal_reason=None,
    )
    timings = [{"action": "status", "passed": True, "elapsed_ms": index} for index in range(95)]
    timings.extend([
        {"action": "preflight", "passed": True},
        {"action": "test", "passed": True, "exit_code": 0},
        {"action": "review", "passed": True},
        {"action": "gate", "passed": True},
        {"action": "learning", "learning_command": "assess"},
    ])
    projected = build_terminal_projection(
        session_state=state, session_timings=timings, files_changed=set(),
        terminal_status="completed", memory_db=tmp_path / "memory.db",
    )
    assert len(projected["observed_actions"]) == 16
    assert len(projected["evidence"]) == 8
    assert all(projected["checks"][name] for name in ("preflight", "test", "review", "gate", "learning"))
    assert all(
        set(reference) == {"observed_action_sha256"}
        for facts in projected["checks"].values() for reference in facts
    )
    assert "84 earlier Anchor action facts omitted by terminal projection bound" in projected["coverage"]["unavailable"]
    assert "32 earlier evidence facts omitted by terminal projection bound" in projected["coverage"]["unavailable"]


def test_terminal_projection_retains_adoption_provenance(tmp_path):
    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=tmp_path, check=True, capture_output=True,
            text=True, encoding="utf-8",
        ).stdout.strip()

    git("init", "-b", "main")
    git("config", "user.email", "tests@example.invalid")
    git("config", "user.name", "Tests")
    git("config", "commit.gpgsign", "false")
    source = tmp_path / "source.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-m", "base")
    from odibi_anchor._repository_snapshot import capture_task_repository_baseline

    baseline = replace(
        capture_task_repository_baseline(tmp_path, "main"),
        authority_kind="adopted",
        adoption_provenance={
            "approval_id": "ada-test", "challenge_sha256": "a" * 64,
            "changed_paths": ["source.py"],
        },
    )
    source.write_text("VALUE = 2\n", encoding="utf-8")
    state = SimpleNamespace(
        task_verification_epoch=0, task_repository_baseline=baseline,
        task_repository_write_fingerprints={}, evidence_ledger=[], managed_artifact_ledger=[],
        artifact_root=str(tmp_path), task_window_id="adopted-task",
        active_project="project:test", linked_problem=None, linked_spec=None,
        linked_work_item=None, session_id="adopted-session", terminal_reason="qualified",
    )

    projected = build_terminal_projection(
        session_state=state, session_timings=[], files_changed={"source.py"},
        terminal_status="blocked", memory_db=tmp_path / "memory.db",
    )

    assert projected["repository"]["baseline_authority"] == "adopted"
    assert projected["repository"]["adoption_provenance"]["approval_id"] == "ada-test"
    assert projected["repository"]["changed_paths"] == ["source.py"]


def test_terminal_projection_is_aggregate_safe_with_five_complete_memory_lifecycles(
    tmp_path, monkeypatch,
):
    """Real task cardinality must fit the shared forensic integrity budget."""
    task_window_id = "five-memory-task"
    memory = {
        "selections": [
            {"selection_id": f"sel-{i}", "memory_id": f"mem-{i}",
             "selected_at": f"2026-09-01T00:00:0{i}Z",
             "query": {"description": "bounded task query", "project": "project:test"},
             "reason": {"matched_query_terms": ["terminal", "memory"]}}
            for i in range(5)
        ],
        "dispositions": [
            {"disposition_id": f"disp-{i}", "selection_id": f"sel-{i}",
             "disposition": "applied", "replacement_memory_id": None,
             "disposed_at": f"2026-09-01T00:01:0{i}Z",
             "reason": {"basis": "applied during exact-runtime dogfood"}}
            for i in range(5)
        ],
        "applications": [
            {"application_id": f"app-{i}", "selection_id": f"sel-{i}",
             "applied_at": f"2026-09-01T00:02:0{i}Z", "action": "retained exact evidence",
             "context": {"scope": "terminal correction"}}
            for i in range(5)
        ],
        "evaluations": [
            {"evaluation_id": f"eval-{i}", "application_id": f"app-{i}",
             "outcome": "helpful", "evaluated_at": f"2026-09-01T00:03:0{i}Z",
             "evidence": {"check": "retained fact"}}
            for i in range(5)
        ],
        "projections": [{
            "projection_id": "proj-1", "learning_item_id": "lrn-1", "memory_id": "mem-new",
            "projected_at": "2026-09-01T00:04:00Z",
            "lineage": {"learning_obligation_id": "lob-1", "assessment_id": "las-1",
                        "evidence_ref_sha256": ["a" * 64], "project_refs": ["project:test"],
                        "recurrence_key": "lrk:v1:test", "recurrence_count": 1,
                        "task_window_id": task_window_id,
                        "producing_task_record": {"status": "unavailable"}},
        }],
    }
    monkeypatch.setattr(
        "odibi_anchor.codebase._memory_lifecycle.task_memory_records",
        lambda *_args, **_kwargs: memory,
    )
    state = SimpleNamespace(
        task_verification_epoch=0, task_repository_baseline=None,
        task_repository_write_fingerprints={}, evidence_ledger=[], managed_artifact_ledger=[],
        artifact_root=str(tmp_path), task_window_id=task_window_id,
        active_project="project:test", linked_problem="PRB-2026-0017", linked_spec="SPEC",
        linked_work_item="WI-2026-0011", session_id="session-five-memory",
        terminal_reason="truthful safe stop",
    )
    timings = [{"action": "status", "passed": True, "elapsed_ms": i} for i in range(35)]
    timings.extend([
        {"action": "preflight", "passed": True}, {"action": "test", "passed": True},
        {"action": "review", "passed": True}, {"action": "gate", "passed": False},
        {"action": "learning", "learning_command": "assess"},
    ])
    projected = build_terminal_projection(
        session_state=state, session_timings=timings, files_changed=set(),
        terminal_status="blocked", assessment={"assessment_id": "las-1"},
        memory_db=tmp_path / "memory.db",
    )
    assert redact_payload(projected) == projected
    persisted = persist_terminal_record(tmp_path / "memory.db", projected)
    assert persisted["record_id"].startswith("ttr_")
    inspected = inspect_terminal_records(
        tmp_path / "memory.db", task_window_id=task_window_id, project_id="project:test",
    )
    assert inspected["records"][0]["valid"] is True
    assert len(inspected["records"][0]["record"]["memory"]["evaluations"]) == 5
