"""Integrated task retrieval, feedback, replay, and storage contracts."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import pytest

from odibi_anchor._dispatcher._memory_actions import (
    _SEED_LOAD_DELETE_TRIGGER_DDL,
    _SEED_LOAD_TABLE_DDL,
    _SEED_LOAD_UPDATE_TRIGGER_DDL,
    _SEED_WITHDRAWAL_DELETE_TRIGGER_DDL,
    _SEED_WITHDRAWAL_TABLE_DDL,
    _SEED_WITHDRAWAL_UPDATE_TRIGGER_DDL,
    build_task_memory_context,
    memory_action,
)
from odibi_anchor._forensic_replay import append_event
from odibi_anchor.codebase._memory_lifecycle import (
    record_application,
    record_selection,
)
from odibi_anchor.codebase.memory_context import append_memory


class _Profile:
    def to_dict(self):
        return {"work_type": "change", "execution_mode": "source_change"}


def _state(tmp_path):
    return SimpleNamespace(
        active_task_profile=_Profile(), active_project="project:test",
        linked_problem="PRB-2026-0010", linked_spec="MEMORY_OPERATING_MODEL_SPEC",
        task_window_id="ltw_memory", memory_selections=[], memory_applications=[],
        anchor_home=str(tmp_path / "anchor"), target_root=str(tmp_path / "target"),
        artifact_root=str(tmp_path / "artifacts"),
        project_root=str(tmp_path / "artifacts"), repository_provider=None,
        trust_domain="private:test",
    )


def test_public_recovery_derives_owner_and_actor_from_session(tmp_path, monkeypatch):
    state = _state(tmp_path)
    observed = {}

    def fake_recovery(_path, **kwargs):
        observed.update(kwargs)
        return {"schema_version": 3, "states": [], "counts": {}}

    monkeypatch.setattr(
        "odibi_anchor.codebase._memory_lifecycle.selection_recovery", fake_recovery,
    )
    result = memory_action(
        tmp_path, ("recovery",), {"action": "inspect", "db_path": str(tmp_path / "memory.db")},
        session_state=state, query_fn=None, render_fn=None,
    )

    assert result["kind"] == "memory_selection_recovery"
    assert observed["project_id"] == "project:test"
    assert observed["current_task_window_id"] == "ltw_memory"
    assert observed["actor_kind"] == "agent"
    assert observed["actor_ref"] == "dispatcher"
    with pytest.raises(ValueError, match="unknown memory recovery arguments"):
        memory_action(
            tmp_path, ("recovery",), {
                "action": "inspect", "project_id": "foreign",
                "db_path": str(tmp_path / "memory.db"),
            }, session_state=state, query_fn=None, render_fn=None,
        )


def test_task_retrieval_apply_and_evaluate_are_separate(tmp_path):
    db = tmp_path / "memory.db"
    append_memory(
        tmp_path, entry_type="gotcha",
        content="Memory lifecycle changes require explicit evidence and compatibility tests.",
        tags=["change"], project="project:test", db_path=str(db),
    )
    state = _state(tmp_path)
    context = build_task_memory_context(
        tmp_path, ("Implement memory lifecycle changes",),
        {"goal": "Preserve compatibility with explicit evidence", "tags": ["change"]},
        session_state=state, task_stage={"repository_scope": ["src/odibi_anchor"]},
        db_path=str(db),
    )
    assert context["selection_count"] == 1
    assert context["retrieval_is_application"] is False
    assert state.memory_applications == []
    assert "description" not in context["query"]
    assert "goal" not in context["query"]
    assert context["query"]["description_present"] is True
    assert context["query"]["goal_present"] is True
    with sqlite3.connect(db) as connection:
        stored_query = connection.execute(
            "SELECT query_json FROM memory_selections"
        ).fetchone()[0]
    assert "Implement memory lifecycle changes" not in stored_query
    assert "Preserve compatibility" not in stored_query

    applied = memory_action(
        tmp_path, ("apply",), {
            "selection_id": context["selections"][0]["selection_id"],
            "action": "design compatibility adapter", "context": {"file": "src/module.py"},
            "db_path": str(db),
        }, session_state=state, query_fn=None, render_fn=None,
    )
    evaluated = memory_action(
        tmp_path, ("evaluate",), {
            "application_id": applied["application_id"], "outcome": "helpful",
            "evidence": {"test": "tests/test_compatibility.py passed"}, "db_path": str(db),
        }, session_state=state, query_fn=None, render_fn=None,
    )
    assert applied["kind"] == "memory_application"
    assert evaluated["outcome"] == "helpful"
    assert state.memory_applications == [applied["application_id"]]


def test_task_retrieval_ignores_nonempty_store_without_material_match(tmp_path):
    db = tmp_path / "memory.db"
    append_memory(
        tmp_path, entry_type="pattern",
        content="Documentation for warehouse partition sizing.",
        tags=["analytics"], related_files=["src/warehouse/**"],
        project="project:test", db_path=str(db),
    )
    state = _state(tmp_path)
    state.active_task_profile = SimpleNamespace(
        to_dict=lambda: {"work_type": "documentation", "execution_mode": "source_change"}
    )

    context = build_task_memory_context(
        tmp_path, ("Clarify API authentication documentation for failures",),
        {"goal": "Explain gateway error responses", "tags": ["security"]},
        session_state=state,
        task_stage={"repository_scope": ["docs/api/authentication.md"]},
        db_path=str(db),
    )

    assert context["selection_count"] == 0
    assert context["selections"] == []
    assert state.memory_selections == []
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT count(*) FROM memory_selections").fetchone()[0] == 0


def test_task_retrieval_ranks_full_scoped_corpus_before_bounding(tmp_path):
    db = tmp_path / "memory.db"
    relevant = append_memory(
        tmp_path, entry_type="pattern",
        content="Exact cohort diagnostics reconcile selection application evaluation rates.",
        project="project:test", db_path=str(db),
    )
    for index in range(65):
        append_memory(
            tmp_path, entry_type="pattern", content=f"Unrelated warehouse note {index}.",
            project="project:test", db_path=str(db),
        )
    state = _state(tmp_path)
    context = build_task_memory_context(
        tmp_path, ("Fix cohort diagnostics",),
        {"goal": "Reconcile selection application evaluation rates"},
        session_state=state, task_stage={"repository_scope": []}, db_path=str(db), limit=1,
    )
    assert context["selection_count"] == 1
    assert context["selections"][0]["memory_id"] == relevant["id"]


def test_task_retrieval_honors_requested_bounded_limit(tmp_path):
    db = tmp_path / "memory.db"
    for index in range(6):
        append_memory(
            tmp_path, entry_type="pattern",
            content=f"Cohort diagnostics reconcile selection evaluation convention {index}.",
            project="project:test", db_path=str(db),
        )
    state = _state(tmp_path)

    context = build_task_memory_context(
        tmp_path, ("Review cohort diagnostics conventions",),
        {"goal": "Reconcile selection evaluation conventions"},
        session_state=state, task_stage={"repository_scope": []}, db_path=str(db), limit=6,
    )

    assert context["selection_count"] == 6
    assert context["bounded_limit"] == 6
    assert len(state.memory_selections) == 6


def test_applied_disposition_rejects_empty_action_without_partial_commit(tmp_path):
    db = tmp_path / "memory.db"
    memory = append_memory(
        tmp_path, entry_type="gotcha", content="Atomic guidance.",
        project="project:test", db_path=str(db),
    )
    state = _state(tmp_path)
    record_selection(
        db, task_window_id=state.task_window_id, query={"goal": "atomic"},
        memory_id=memory["id"], reason={"match": "goal"},
    )
    with pytest.raises(ValueError, match="action"):
        memory_action(
            tmp_path, ("disposition",), {
                "memory_id": memory["id"], "disposition": "applied",
                "reason": {"basis": "relevant"}, "action": "", "db_path": str(db),
            }, session_state=state, query_fn=None, render_fn=None,
        )
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT count(*) FROM memory_dispositions").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM memory_applications").fetchone()[0] == 0


def test_seeded_memory_dogfood_disposition_evaluation_and_diagnostics(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "odibi_anchor.human_input_owner.owner_approval_provider_status",
        lambda: {
            "available": True,
            "configured": True,
            "transport": "local-windows-owner-presence",
            "assurance": "interactive_local_windows_account_presence",
            "reason": None,
        },
    )
    db = tmp_path / "memory.db"
    state = _state(tmp_path)
    loaded = memory_action(
        tmp_path, ("seed",), {"command": "load", "db_path": str(db)},
        session_state=state, query_fn=None, render_fn=None,
    )
    assert isinstance(loaded, dict)
    assert loaded["entry_count"] == 10
    assert {item["id"] for item in loaded["withdrawn"]} == {
        "seed_agent_reliability_03", "seed_agent_reliability_08", "seed_agent_reliability_09",
    }
    assert {item["status"] for item in loaded["entries"]} == {"candidate"}
    assert {item["human_review"] for item in loaded["entries"]} == {None}
    assert loaded["seed_load_event_id"].startswith("seedload_")
    assert {item["action"] for item in loaded["loaded"]} == {"inserted"}
    repeated = memory_action(
        tmp_path, ("seed",), {"command": "load", "db_path": str(db)},
        session_state=state, query_fn=None, render_fn=None,
    )
    assert isinstance(repeated, dict)
    assert {item["action"] for item in repeated["loaded"]} == {"unchanged"}
    assert repeated["seed_load_event_id"] == loaded["seed_load_event_id"]
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT count(*) FROM memory_seed_load_events").fetchone()[0] == 1

    context = build_task_memory_context(
        tmp_path, ("Harden quality baseline analyzer version drift",),
        {"goal": "Fail closed when quality analyzers drift", "tags": ["quality"]},
        session_state=state, task_stage={"repository_scope": ["scripts/quality_ratchet.py"]},
        db_path=str(db), limit=3,
    )
    selected = next(item for item in context["selections"] if item["memory_id"] == "seed_agent_reliability_10")

    disposed = memory_action(
        tmp_path, ("disposition",), {
            "memory_id": selected["memory_id"], "disposition": "applied",
            "reason": {"basis": "matched analyzer drift"}, "action": "design fail-closed check",
            "db_path": str(db),
        }, session_state=state, query_fn=None, render_fn=None,
    )
    assert isinstance(disposed, dict)
    assert disposed["results"][0]["application"] is not None
    evaluated = memory_action(
        tmp_path, ("evaluate",), {
            "memory_id": selected["memory_id"], "outcome": "helpful",
            "evidence": {"test": "quality ratchet test passed"}, "db_path": str(db),
        }, session_state=state, query_fn=None, render_fn=None,
    )
    assert isinstance(evaluated, dict)
    assert evaluated["outcome"] == "helpful"
    diagnostics = memory_action(
        tmp_path, ("diagnostics",), {"project": "project:test", "db_path": str(db)},
        session_state=state, query_fn=None, render_fn=None,
    )
    assert isinstance(diagnostics, dict)
    assert diagnostics["counts"]["outcomes"] == {"helpful": 1}
    assert diagnostics["rates"]["retrieval_to_disposition"]["numerator"] == 1
    assert diagnostics["counts"]["durable_facts"] == {
        "promotion_events": 0, "promoted_memories": 0, "rejected_memories": 0,
        "promotion_shadow_decisions": 0, "promotion_attestations": 0,
        "promotion_human_receipts": 0,
        "promotion_verifier_runs": 0,
        "quarantined_memories": 0, "stale_memories": 0, "superseded_memories": 0,
        "reviewed_seed_loads": 1,
        "reviewed_seed_withdrawals": 0, "reviewed_seed_reconciliations": 0,
    }
    assert diagnostics["bounded_ids"]["promoted"] == []
    assert diagnostics["bounded_ids"]["stale"] == []
    assert diagnostics["promotion_capability"] == {
        "status": "machine_and_owner_promotion_enabled",
        "reason": "verifier_attestations_or_governed_owner_receipts",
        "policy_version": diagnostics["promotion_shadow"]["policy_version"],
        "policy_sha256": diagnostics["promotion_shadow"]["policy_sha256"],
        "automatic_candidate_activation_enabled": True,
        "automatic_active_confirmation_enabled": True,
        "authenticated_human_provider_available": False,
        "authenticated_human_provider_configured": False,
        "human_owner_provider_available": True,
        "human_owner_provider_configured": True,
        "human_owner_provider_transport": "local-windows-owner-presence",
        "human_owner_provider_assurance": "interactive_local_windows_account_presence",
    }


def test_reviewed_seed_retrieval_uses_discriminative_signals(tmp_path):
    db = tmp_path / "memory.db"
    state = _state(tmp_path)
    loaded = memory_action(
        tmp_path, ("seed",), {"command": "load", "db_path": str(db)},
        session_state=state, query_fn=None, render_fn=None,
    )
    assert isinstance(loaded, dict)
    cases = {
        "seed_agent_reliability_11": {
            "description": "A gateway request loses a required argument; reproduce it from a captured payload before editing the dispatcher.",
            "goal": "Separate transport behavior from application behavior.",
            "tags": ["debugging", "reproduction"],
            "files": ["tests/debugging/test_error_trace_context.py"],
            "competitors": {"seed_agent_reliability_02", "seed_agent_reliability_04"},
        },
        "seed_agent_reliability_12": {
            "description": "Security review found a second entrypoint can mint elevated memory state after the primary endpoint was fixed.",
            "goal": "Check direct append and bulk Markdown ingestion before declaring remediation complete.",
            "tags": ["authority", "attack-surface"],
            "files": [
                "src/odibi_anchor/codebase/_memory_db.py",
                "src/odibi_anchor/codebase/memory_context.py",
            ],
            "competitors": {"seed_agent_reliability_05", "seed_agent_reliability_13"},
        },
        "seed_agent_reliability_13": {
            "description": "A patch changed after reviewer feedback. Assess the revised commit while retaining prior findings.",
            "goal": "Return a recommendation without performing repository writes.",
            "tags": ["review", "exact-head"],
            "files": [".assistant/references/reviewing/independent-pr-review.md"],
            "competitors": {"seed_agent_reliability_01", "seed_agent_reliability_07"},
        },
    }
    for index, (seed_id, case) in enumerate(cases.items()):
        retrieval_state = _state(tmp_path)
        retrieval_state.task_window_id = f"ltw_discriminative_{index}"
        retrieved = build_task_memory_context(
            tmp_path, (case["description"],),
            {"goal": case["goal"], "tags": case["tags"]},
            session_state=retrieval_state,
            task_stage={"repository_scope": case["files"]}, db_path=str(db),
            limit=loaded["entry_count"],
        )
        ranked_ids = [item["memory_id"] for item in retrieved["selections"]]
        assert ranked_ids[0] == seed_id
        assert all(
            other not in ranked_ids or ranked_ids.index(seed_id) < ranked_ids.index(other)
            for other in case["competitors"]
        )
        reason = retrieved["selections"][0]["reason"]
        assert reason["matched_tags"] == sorted(case["tags"])
        assert reason["matched_files"] == sorted(case["files"])

    unrelated_state = _state(tmp_path)
    unrelated_state.task_window_id = "ltw_unrelated_control"
    unrelated = build_task_memory_context(
        tmp_path, ("Optimize parquet aggregation latency for monthly dashboards",),
        {"goal": "Benchmark vectorized throughput", "tags": ["performance"]},
        session_state=unrelated_state,
        task_stage={"repository_scope": ["src/analytics/monthly_rollup.py"]},
        db_path=str(db), limit=loaded["entry_count"],
    )
    assert unrelated["selections"] == []


def test_seed_load_event_is_exact_immutable_and_idempotent(tmp_path):
    db = tmp_path / "memory.db"
    state = _state(tmp_path)
    first = memory_action(
        tmp_path, ("seed",), {"command": "load", "db_path": str(db)},
        session_state=state, query_fn=None, render_fn=None,
    )
    second = memory_action(
        tmp_path, ("seed",), {"command": "load", "db_path": str(db)},
        session_state=state, query_fn=None, render_fn=None,
    )
    assert isinstance(first, dict) and isinstance(second, dict)
    assert first["seed_load_event_id"] == second["seed_load_event_id"]
    with sqlite3.connect(db) as connection:
        objects = dict(connection.execute(
            "SELECT name,sql FROM sqlite_master WHERE name IN (?,?,?)",
            ("memory_seed_load_events", "memory_seed_load_events_no_update",
             "memory_seed_load_events_no_delete"),
        ))
        assert objects["memory_seed_load_events"].replace(" IF NOT EXISTS", "") == _SEED_LOAD_TABLE_DDL
        assert objects["memory_seed_load_events_no_update"].replace(" IF NOT EXISTS", "") == _SEED_LOAD_UPDATE_TRIGGER_DDL
        assert objects["memory_seed_load_events_no_delete"].replace(" IF NOT EXISTS", "") == _SEED_LOAD_DELETE_TRIGGER_DDL
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("UPDATE memory_seed_load_events SET entry_count=0")
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("DELETE FROM memory_seed_load_events")


@pytest.mark.parametrize("drift", ["table", "trigger"])
def test_seed_load_rejects_old_schema_drift(tmp_path, drift):
    db = tmp_path / "memory.db"
    state = _state(tmp_path)
    from odibi_anchor.codebase._memory_db import get_db

    connection = get_db(db)
    if drift == "table":
        connection.execute(
            "CREATE TABLE memory_seed_load_events (event_id TEXT PRIMARY KEY,manifest_sha256 TEXT NOT NULL)"
        )
    else:
        connection.execute(_SEED_LOAD_TABLE_DDL)
        connection.execute(
            "CREATE TRIGGER memory_seed_load_events_no_update BEFORE UPDATE ON "
            "memory_seed_load_events BEGIN SELECT RAISE(ABORT,'wrong'); END"
        )
    connection.commit()
    with pytest.raises(RuntimeError, match="schema mismatch"):
        memory_action(
            tmp_path, ("seed",), {"command": "load", "db_path": str(db)},
            session_state=state, query_fn=None, render_fn=None,
        )


def test_seed_load_rejects_existing_event_conflict(tmp_path):
    db = tmp_path / "memory.db"
    state = _state(tmp_path)
    from odibi_anchor.codebase._memory_db import get_db

    inspected = memory_action(
        tmp_path, ("seed",), {"command": "inspect", "db_path": str(db)},
        session_state=state, query_fn=None, render_fn=None,
    )
    assert isinstance(inspected, dict)
    connection = get_db(db)
    connection.execute(_SEED_LOAD_TABLE_DDL)
    event_id = "seedload_" + __import__("hashlib").sha256(
        inspected["manifest_sha256"].encode()
    ).hexdigest()
    connection.execute(
        "INSERT INTO memory_seed_load_events VALUES(?,?,?,?,?)",
        (event_id, inspected["manifest_sha256"], 999, "[]", "2026-01-01T00:00:00Z"),
    )
    connection.commit()
    with pytest.raises(RuntimeError, match="conflicting memory seed load event"):
        memory_action(
            tmp_path, ("seed",), {"command": "load", "db_path": str(db)},
            session_state=state, query_fn=None, render_fn=None,
        )


def test_seed_load_reconciles_absent_managed_seeds_without_mutating_inspect(tmp_path):
    db = tmp_path / "memory.db"
    state = _state(tmp_path)
    from odibi_anchor.codebase._memory_db import get_db

    connection = get_db(db)
    now = "2026-01-01T00:00:00Z"
    old_ids = {
        "seed_agent_reliability_03", "seed_agent_reliability_08", "seed_agent_reliability_09",
    }
    for seed_id in sorted(old_ids):
        connection.execute(
            "INSERT INTO memories(id,project,type,content,related_files,tags,source,confidence,status,evidence,created,last_used) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (seed_id, "all", "convention", f"Old guidance {seed_id}", "[]", "[]",
             f"reviewed-seed:{seed_id}", 0.5, "candidate", "{}", now, now),
        )
        connection.execute(
            "INSERT INTO memory_fts(id,content,tags) VALUES(?,?,?)",
            (seed_id, f"Old guidance {seed_id}", "[]"),
        )
    connection.commit()
    connection.close()
    user = append_memory(
        tmp_path, entry_type="convention", content="User-owned optional runtime guidance.",
        tags=["runtime"], project="all", db_path=str(db),
    )

    inspected = memory_action(
        tmp_path, ("seed",), {"command": "inspect", "db_path": str(db)},
        session_state=state, query_fn=None, render_fn=None,
    )
    assert isinstance(inspected, dict)
    with sqlite3.connect(db) as connection:
        assert connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE name='memory_seed_withdrawal_events'"
        ).fetchone()[0] == 0
        assert dict(connection.execute(
            "SELECT id,status FROM memories WHERE id IN (?,?,?)", tuple(sorted(old_ids)),
        )) == {seed_id: "candidate" for seed_id in old_ids}

    loaded = memory_action(
        tmp_path, ("seed",), {"command": "load", "db_path": str(db)},
        session_state=state, query_fn=None, render_fn=None,
    )
    assert isinstance(loaded, dict)
    assert {item["id"] for item in loaded["reconciled_withdrawals"]} == old_ids
    first_events = {item["id"]: item["event_id"] for item in loaded["reconciled_withdrawals"]}
    repeated = memory_action(
        tmp_path, ("seed",), {"command": "load", "db_path": str(db)},
        session_state=state, query_fn=None, render_fn=None,
    )
    assert isinstance(repeated, dict)
    assert {item["id"]: item["event_id"] for item in repeated["reconciled_withdrawals"]} == first_events

    with sqlite3.connect(db) as connection:
        assert dict(connection.execute(
            "SELECT id,status FROM memories WHERE id IN (?,?,?)", tuple(sorted(old_ids)),
        )) == {seed_id: "quarantined" for seed_id in old_ids}
        assert connection.execute("SELECT status FROM memories WHERE id=?", (user["id"],)).fetchone()[0] == "candidate"
        assert connection.execute("SELECT count(*) FROM memory_seed_withdrawal_events").fetchone()[0] == 3
        objects = dict(connection.execute(
            "SELECT name,sql FROM sqlite_master WHERE name IN (?,?,?)",
            ("memory_seed_withdrawal_events", "memory_seed_withdrawal_events_no_update",
             "memory_seed_withdrawal_events_no_delete"),
        ))
        assert objects["memory_seed_withdrawal_events"].replace(" IF NOT EXISTS", "") == _SEED_WITHDRAWAL_TABLE_DDL
        assert objects["memory_seed_withdrawal_events_no_update"].replace(" IF NOT EXISTS", "") == _SEED_WITHDRAWAL_UPDATE_TRIGGER_DDL
        assert objects["memory_seed_withdrawal_events_no_delete"].replace(" IF NOT EXISTS", "") == _SEED_WITHDRAWAL_DELETE_TRIGGER_DDL
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("UPDATE memory_seed_withdrawal_events SET reason='rewrite'")
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("DELETE FROM memory_seed_withdrawal_events")

    restarted = _state(tmp_path)
    restarted.task_window_id = "ltw_after_seed_upgrade"
    context = build_task_memory_context(
        tmp_path, ("Qualify optional runtime guidance",), {"goal": "runtime guidance"},
        session_state=restarted, task_stage={"repository_scope": []}, db_path=str(db), limit=10,
    )
    selected_ids = {item["memory_id"] for item in context["selections"]}
    assert selected_ids.isdisjoint(old_ids)
    assert user["id"] in selected_ids
    restart_script = """
import json, sys
from types import SimpleNamespace
from odibi_anchor._dispatcher._memory_actions import build_task_memory_context
state = SimpleNamespace(
    active_task_profile=None, active_project='project:test', linked_problem=None,
    linked_spec=None, task_window_id='ltw_subprocess_restart', memory_selections=[],
    memory_applications=[],
)
result = build_task_memory_context(
    '.', ('Qualify optional runtime guidance',), {'goal': 'runtime guidance'},
    session_state=state, task_stage={'repository_scope': []}, db_path=sys.argv[1], limit=10,
)
print(json.dumps(sorted(item['memory_id'] for item in result['selections'])))
"""
    restarted_process = subprocess.run(
        [sys.executable, "-c", restart_script, str(db)],
        check=True, capture_output=True, text=True,
    )
    process_ids = set(json.loads(restarted_process.stdout))
    assert process_ids.isdisjoint(old_ids)
    assert user["id"] in process_ids
    diagnostics = memory_action(
        tmp_path, ("diagnostics",), {"project": "project:test", "db_path": str(db)},
        session_state=restarted, query_fn=None, render_fn=None,
    )
    assert isinstance(diagnostics, dict)
    assert diagnostics["counts"]["durable_facts"]["reviewed_seed_withdrawals"] == 3
    assert set(diagnostics["bounded_ids"]["withdrawn_reviewed_seeds"]) == old_ids


def test_seed_load_rejects_conflicting_managed_source_atomically(tmp_path):
    db = tmp_path / "memory.db"
    state = _state(tmp_path)
    from odibi_anchor.codebase._memory_db import get_db

    connection = get_db(db)
    now = "2026-01-01T00:00:00Z"
    connection.execute(
        "INSERT INTO memories(id,project,type,content,related_files,tags,source,confidence,status,evidence,created,last_used) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        ("seed_agent_reliability_08", "all", "convention", "Old seed", "[]", "[]",
         "reviewed-seed:seed_agent_reliability_09", 0.5, "candidate", "{}", now, now),
    )
    connection.commit()
    connection.close()
    with pytest.raises(ValueError, match="conflicting reviewed seed source"):
        memory_action(
            tmp_path, ("seed",), {"command": "load", "db_path": str(db)},
            session_state=state, query_fn=None, render_fn=None,
        )
    with sqlite3.connect(db) as connection:
        assert connection.execute(
            "SELECT status FROM memories WHERE id='seed_agent_reliability_08'"
        ).fetchone()[0] == "candidate"
        assert connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE name='memory_seed_withdrawal_events'"
        ).fetchone()[0] == 0


def test_harmful_memory_is_quarantined_and_project_boundary_is_preserved(tmp_path):
    db = tmp_path / "memory.db"
    local = append_memory(
        tmp_path, entry_type="gotcha", content="Local stale deployment advice.",
        tags=["deployment"], project="project:test", db_path=str(db),
    )
    append_memory(
        tmp_path, entry_type="gotcha", content="Private other-project deployment secret.",
        tags=["deployment"], project="private:other", db_path=str(db),
    )
    state = _state(tmp_path)
    context = build_task_memory_context(
        tmp_path, ("Review deployment advice",), {"goal": "deployment"},
        session_state=state, task_stage={"repository_scope": []}, db_path=str(db),
    )
    assert {item["memory_id"] for item in context["selections"]} == {local["id"]}
    applied = memory_action(
        tmp_path, ("disposition",), {
            "memory_id": local["id"], "disposition": "applied",
            "reason": {"basis": "appeared relevant"}, "action": "review advice",
            "db_path": str(db),
        }, session_state=state, query_fn=None, render_fn=None,
    )
    assert isinstance(applied, dict)
    application = applied["results"][0]["application"]
    assert application is not None
    memory_action(
        tmp_path, ("evaluate",), {
            "application_id": application["application_id"],
            "outcome": "harmful", "evidence": {"test": "contradicted by current source"},
            "db_path": str(db),
        }, session_state=state, query_fn=None, render_fn=None,
    )
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT status FROM memories WHERE id=?", (local["id"],)).fetchone()[0] == "quarantined"
    later = _state(tmp_path)
    later.task_window_id = "ltw_later"
    retrieved = build_task_memory_context(
        tmp_path, ("Review deployment advice",), {"goal": "deployment"},
        session_state=later, task_stage={"repository_scope": []}, db_path=str(db),
    )
    assert retrieved["selection_count"] == 0


def test_unresolved_project_retrieves_only_explicitly_global_memory(tmp_path):
    db = tmp_path / "memory.db"
    global_memory = append_memory(
        tmp_path, entry_type="pattern", content="Global public deployment practice.",
        tags=["deployment"], project="all", db_path=str(db),
    )
    append_memory(
        tmp_path, entry_type="gotcha", content="Private unresolved deployment detail.",
        tags=["deployment"], project="private:other", db_path=str(db),
    )
    state = _state(tmp_path)
    state.active_project = None
    context = build_task_memory_context(
        tmp_path, ("Review deployment guidance",),
        {"goal": "deployment", "tags": ["deployment"]},
        session_state=state, task_stage={"repository_scope": []}, db_path=str(db),
    )
    assert {item["memory_id"] for item in context["selections"]} == {global_memory["id"]}


@pytest.mark.parametrize(
    ("active", "requested"),
    [("private:a", "private:b"), (None, "private:a")],
)
def test_ordinary_query_rejects_project_override(tmp_path, active, requested):
    state = _state(tmp_path)
    state.active_project = active
    called = []
    with pytest.raises(ValueError, match="active project/trust boundary"):
        memory_action(
            tmp_path, ("deployment",), {"project": requested}, session_state=state,
            query_fn=lambda *args, **kwargs: called.append(kwargs), render_fn=lambda value: value,
        )
    assert called == []


def test_unresolved_ordinary_query_is_forced_global(tmp_path):
    state = _state(tmp_path)
    state.active_project = None
    called = []
    result = memory_action(
        tmp_path, ("deployment",), {}, session_state=state,
        query_fn=lambda *args, **kwargs: called.append(kwargs) or {"entries": []},
        render_fn=lambda value: value,
    )
    assert result == {"entries": []}
    assert called[0]["project"] == "all"


@pytest.mark.parametrize(
    "injected",
    ("owner_user_id", "challenge", "receipt", "response_message_id"),
)
def test_owner_promotion_rejects_caller_injected_authority(tmp_path, injected):
    state = _state(tmp_path)
    with pytest.raises(ValueError, match=r'Run anchor\("help", "memory"\)'):
        memory_action(
            tmp_path, ("promotion",), {
                "command": "request_owner_activation", "memory_id": "memory-1",
                injected: "caller-controlled", "db_path": str(tmp_path / "memory.db"),
            }, session_state=state, query_fn=None, render_fn=None,
        )


@pytest.mark.parametrize("subcommand", ("apply", "disposition", "evaluate"))
def test_memory_lifecycle_argument_errors_link_to_help(tmp_path, subcommand):
    with pytest.raises(ValueError, match=r'Run anchor\("help", "memory"\)'):
        memory_action(
            tmp_path, (subcommand,), {
                "not_a_parameter": True, "db_path": str(tmp_path / "memory.db"),
            }, session_state=_state(tmp_path), query_fn=None, render_fn=None,
        )


@pytest.mark.parametrize(
    ("subcommand", "payload", "field"),
    (
        ("apply", {"selection_id": "missing", "action": "use", "context": "not-json"}, "context"),
        ("disposition", {"selection_id": "missing", "disposition": "irrelevant", "reason": "not-json"}, "reason"),
        ("evaluate", {"application_id": "missing", "outcome": "helpful", "evidence": "not-json"}, "evidence"),
    ),
)
def test_memory_lifecycle_rejects_wrong_object_types_before_resolution(
    tmp_path, subcommand, payload, field,
):
    with pytest.raises(
        ValueError,
        match=rf'{field} must be a .*JSON object.*Run anchor\("help", "memory"\)',
    ):
        memory_action(
            tmp_path, (subcommand,), {**payload, "db_path": str(tmp_path / "memory.db")},
            session_state=_state(tmp_path), query_fn=None, render_fn=None,
        )


def test_dispatcher_passes_only_exact_prepared_databricks_approval(tmp_path, monkeypatch):
    db = tmp_path / "memory.db"
    memory = append_memory(
        tmp_path, entry_type="convention", content="Dispatcher Databricks approval.",
        project="project:test", db_path=str(db),
    )
    monkeypatch.setenv("ANCHOR_SLACK_BOT_TOKEN", "token")
    monkeypatch.setenv("ANCHOR_SLACK_CHANNEL_ID", "C123")
    monkeypatch.setenv("ANCHOR_SLACK_USER_ID", "U123")
    monkeypatch.setenv("DATABRICKS_RUNTIME_VERSION", "serverless")
    monkeypatch.setattr("odibi_anchor.human_input_owner._is_windows", lambda: False)
    monkeypatch.setenv("ANCHOR_HUMAN_INPUT_STATE_PATH", str(tmp_path / "human-input.db"))
    state = _state(tmp_path)
    payload = {
        "command": "request_owner_activation", "memory_id": memory["id"],
        "provider": "databricks_in_session", "db_path": str(db),
    }

    prepared = memory_action(
        tmp_path, ("promotion",), payload,
        session_state=state, query_fn=None, render_fn=None,
    )
    recorded = memory_action(
        tmp_path, ("promotion",), {
            **payload, "in_session_approval": prepared["approval_response"],
        }, session_state=state, query_fn=None, render_fn=None,
    )

    assert prepared["status"] == "approval_required"
    assert recorded["status"] == "recorded"
    assert recorded["receipt"]["challenge_sha256"] == prepared["challenge_sha256"]


def test_dispatcher_rejects_unsupported_owner_provider(tmp_path):
    from odibi_anchor.human_input import HumanInputConfigurationError

    with pytest.raises(
        HumanInputConfigurationError,
        match="must be omitted or 'databricks_in_session'",
    ):
        memory_action(
            tmp_path, ("promotion",), {
                "command": "request_owner_activation", "memory_id": "memory-1",
                "provider": "caller-controlled", "db_path": str(tmp_path / "memory.db"),
            }, session_state=_state(tmp_path), query_fn=None, render_fn=None,
        )


def test_diagnostics_and_terminal_records_cannot_cross_active_project(tmp_path):
    from odibi_anchor.codebase._task_execution import FORMAT, UNOBSERVED, persist_terminal_record

    db = tmp_path / "memory.db"
    state = _state(tmp_path)
    persist_terminal_record(db, {
        "format": FORMAT,
        "identities": {
            "task_window_id": "private-task", "session_id": "session-private",
            "project_id": "private:other", "problem_id": None, "spec_id": None,
            "work_item_id": None,
        },
        "started_at": "2026-09-01T00:00:00Z", "ended_at": "2026-09-01T00:01:00Z",
        "repository": {"start_revision": None, "end_revision": None},
        "terminal": {"status": "blocked"},
        "coverage": {
            "unobserved": list(UNOBSERVED), "unavailable": ["repository unavailable"],
            "replay_claim": "none",
        },
    })
    with pytest.raises(ValueError, match="active project/trust boundary"):
        memory_action(
            tmp_path, ("task_record",), {"project_id": "private:other", "db_path": str(db)},
            session_state=state, query_fn=None, render_fn=None,
        )
    visible = memory_action(
        tmp_path, ("task_record",), {"db_path": str(db)},
        session_state=state, query_fn=None, render_fn=None,
    )
    assert isinstance(visible, dict)
    assert visible["records"] == []
    with pytest.raises(ValueError, match="active project/trust boundary"):
        memory_action(
            tmp_path, ("diagnostics",), {"project": "private:other", "db_path": str(db)},
            session_state=state, query_fn=None, render_fn=None,
        )


def test_diagnostics_are_schema_verified_and_selected_never_applied_is_exact(tmp_path):
    db = tmp_path / "memory.db"
    state = _state(tmp_path)
    memory = append_memory(
        tmp_path, entry_type="gotcha", content="Use once.", project="project:test", db_path=str(db),
    )
    first = record_selection(
        db, task_window_id=state.task_window_id, query={"goal": "first"},
        memory_id=memory["id"], reason={"match": "goal"},
    )
    record_application(
        db, selection_id=first["selection_id"], task_window_id=state.task_window_id, action="apply",
    )
    second = record_selection(
        db, task_window_id="later", query={"goal": "later"},
        memory_id=memory["id"], reason={"match": "goal"},
    )
    diagnostics = memory_action(
        tmp_path, ("diagnostics",), {"db_path": str(db)},
        session_state=state, query_fn=None, render_fn=None,
    )
    assert isinstance(diagnostics, dict)
    assert diagnostics["counts"]["selected_never_applied"] == 1
    assert diagnostics["bounded_ids"]["selected_never_applied"] == [second["selection_id"]]
    assert "eligible_learning_to_projection" in diagnostics["rates"]
    with sqlite3.connect(db) as connection:
        connection.execute("DROP INDEX idx_memory_applications_task")
    with pytest.raises(RuntimeError, match="schema checksum mismatch"):
        memory_action(
            tmp_path, ("diagnostics",), {"db_path": str(db)},
            session_state=state, query_fn=None, render_fn=None,
        )


def test_diagnostics_do_not_invent_promotion_events_and_report_stale_ids(tmp_path):
    db = tmp_path / "memory.db"
    state = _state(tmp_path)
    historical = append_memory(
        tmp_path, entry_type="gotcha", content="Historical confirmed state.",
        project="project:test", db_path=str(db),
    )
    stale = append_memory(
        tmp_path, entry_type="gotcha", content="Historical stale state.",
        project="project:test", db_path=str(db),
    )
    record_selection(
        db, task_window_id=state.task_window_id, query={"goal": "initialize lifecycle"},
        memory_id=historical["id"], reason={"match": "test fixture"},
    )
    with sqlite3.connect(db) as connection:
        connection.execute(
            "UPDATE memories SET status='confirmed',confirmation_count=2 WHERE id=?",
            (historical["id"],),
        )
        connection.execute("UPDATE memories SET status='stale' WHERE id=?", (stale["id"],))
    diagnostics = memory_action(
        tmp_path, ("diagnostics",), {"db_path": str(db)},
        session_state=state, query_fn=None, render_fn=None,
    )
    assert isinstance(diagnostics, dict)
    facts = diagnostics["counts"]["durable_facts"]
    assert facts["promotion_events"] == 0
    assert facts["promoted_memories"] == 0
    assert diagnostics["bounded_ids"]["promoted"] == []
    assert facts["stale_memories"] == 1
    assert diagnostics["bounded_ids"]["stale"] == [stale["id"]]


def test_projection_rate_uses_exact_eligible_assessed_observation_cohort(tmp_path, monkeypatch):
    from odibi_anchor.codebase import structured_learning_context as learning

    db = tmp_path / "memory.db"
    monkeypatch.setenv("ANCHOR_MEMORY_DB", str(db))
    state = _state(tmp_path)
    obligation = learning.ensure_learning_obligation(
        task_window_id=state.task_window_id, session_ref="session:test",
        checkpoint_ref="gate:test", project_ref="project:test",
    )

    def capture(observation_type, suffix, scope="project_local"):
        result = learning._structured_learning_dispatch(
            command="capture", _obligation_id=obligation["obligation_id"],
            _project_id="project:test", _task_window_id=state.task_window_id,
            observation_type=observation_type, summary=f"Observed bounded fact {suffix}.",
            signal_key=f"diagnostics.{suffix}", impact="medium",
            applicability_scope=scope, project_refs=["project:test"],
            work_package_refs=[], environment_refs=[],
            provenance={"source_action": "test", "source_version": "v1"},
            evidence=[{"reference_type": "test", "reference": "tests/test_memory_actions.py"}],
        )
        assert isinstance(result, dict)
        return result["item"]

    eligible = capture("reusable_practice", "eligible")
    friction = capture("friction", "friction", "workbench")
    learning._structured_learning_dispatch(
        command="assess", _obligation_id=obligation["obligation_id"],
        _project_id="project:test", _task_window_id=state.task_window_id,
        outcome="observations_recorded",
        observation_ids=[eligible["item_id"], friction["item_id"]],
    )
    derived = learning.structured_learning_context(
        command="triage", decision="derive_lesson",
        source_item_ids=[friction["item_id"]],
        expected_source_versions={friction["item_id"]: friction["version"]},
        summary="Human-triaged friction projection must not enter the eligible cohort.",
        impact="medium", applicability_scope="project_local", project_refs=["project:test"],
        work_package_refs=[], environment_refs=[],
        provenance={"source_action": "review", "source_version": "v1"},
        evidence=[{"reference_type": "test", "reference": "tests/test_memory_actions.py"}],
        actor_kind="human", actor_ref="reviewer:test", decision_source="review:test",
        rationale="Classified only to test exact diagnostic cohort semantics.",
    )
    assert isinstance(derived, dict)
    assert derived["semantic_projection"]
    diagnostics = memory_action(
        tmp_path, ("diagnostics",), {"db_path": str(db)},
        session_state=state, query_fn=None, render_fn=None,
    )
    assert isinstance(diagnostics, dict)
    rate = diagnostics["rates"]["eligible_learning_to_projection"]
    assert rate == {"numerator": 1, "denominator": 1, "rate": 1.0}
    assert rate["rate"] <= 1
    assert diagnostics["counts"]["projections"] == 2


def test_replay_and_storage_reads_use_the_selected_database(tmp_path):
    db = tmp_path / "memory.db"
    state = _state(tmp_path)
    append_event(
        db, state.task_window_id, "evidence", {"result": "verified"}, event_id="evidence-1",
    )
    replay = memory_action(
        tmp_path, ("replay",), {"view": "context", "db_path": str(db)},
        session_state=state, query_fn=None, render_fn=None,
    )
    storage = memory_action(
        tmp_path, ("storage",), {"command": "inspect", "db_path": str(db)},
        session_state=state, query_fn=None, render_fn=None,
    )
    assert replay["verification"]["valid"] is True
    assert replay["events"][0]["payload"] == {"result": "verified"}
    assert storage["path"] == str(db)
    assert storage["integrity"] == "ok"


def test_historical_replay_requires_terminal_ownership_in_active_boundary(tmp_path):
    from odibi_anchor.codebase._task_execution import FORMAT, UNOBSERVED, persist_terminal_record

    db = tmp_path / "memory.db"
    state = _state(tmp_path)
    persist_terminal_record(db, {
        "format": FORMAT,
        "identities": {
            "task_window_id": "private-other-task", "session_id": "session-private",
            "project_id": "private:other", "problem_id": None, "spec_id": None,
            "work_item_id": None,
        },
        "started_at": "2026-09-01T00:00:00Z", "ended_at": "2026-09-01T00:01:00Z",
        "repository": {"start_revision": None, "end_revision": None},
        "terminal": {"status": "blocked"},
        "coverage": {
            "unobserved": list(UNOBSERVED), "unavailable": ["repository unavailable"],
            "replay_claim": "none",
        },
    })
    append_event(
        db, "private-other-task", "evidence", {"private": "fact"}, event_id="private-evidence",
    )
    for task in ("private-other-task", "arbitrary-unowned-task"):
        with pytest.raises(ValueError, match="active project/trust boundary"):
            memory_action(
                tmp_path, ("replay",), {"task_window_id": task, "db_path": str(db)},
                session_state=state, query_fn=None, render_fn=None,
            )


def test_boot_restores_durable_task_memory_activity(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    db = tmp_path / "memory.db"
    memory = append_memory(
        root, entry_type="gotcha", content="Restore this task memory.",
        project="project:test", db_path=str(db),
    )
    selected = record_selection(
        db, task_window_id="task-restart", query={"goal": "restore"},
        memory_id=memory["id"], reason={"match": "goal"},
    )
    applied = record_application(
        db, selection_id=selected["selection_id"], task_window_id="task-restart",
        action="verify restart",
    )
    (root / ".anchor_session_state.json").write_text(
        json.dumps({"task_window_id": "task-restart", "awaiting_learn": False}),
        encoding="utf-8",
    )
    script = """
import json, sys
from odibi_anchor._dispatcher._boot import run_boot
from odibi_anchor._utils._session_state import get_state
run_boot(sys.argv[1], sys.argv[2], state_root=sys.argv[1], db_path=sys.argv[3],
         frame_enabled=False, learning_recovery_checked=True)
print("RESULT=" + json.dumps(get_state()))
"""
    environment = {**os.environ, "ANCHOR_MEMORY_DB": str(db)}
    completed = subprocess.run(
        [sys.executable, "-c", script, str(root), str(tmp_path), str(db)],
        check=True, capture_output=True, text=True, env=environment,
    )
    state = json.loads(next(
        line.removeprefix("RESULT=") for line in completed.stdout.splitlines()
        if line.startswith("RESULT=")
    ))
    assert state["memory_selections"] == [selected["selection_id"]]
    assert state["memory_applications"] == [applied["application_id"]]


def _repackaged_manifest(**entry_overrides):
    """Return the packaged manifest with entry edits and BOTH digests recomputed.

    This models a tamperer with write access to the installed resource who is
    competent enough to re-checksum: `manifest_sha256` is an integrity control, not
    an authenticity one, so it will verify cleanly. Every trust-bearing property must
    therefore be re-validated independently (WI-2026-0019).
    """
    import hashlib
    import pathlib

    from odibi_anchor._dispatcher import _memory_actions

    resource = pathlib.Path(_memory_actions.__file__).resolve().parent.parent / "seeds" / "agent_reliability.json"
    manifest = json.loads(resource.read_text(encoding="utf-8"))
    manifest["entries"][0].update(entry_overrides)
    for entry in manifest["entries"]:
        body = {key: entry[key] for key in entry if key != "content_sha256"}
        entry["content_sha256"] = hashlib.sha256(json.dumps(
            body, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ).encode()).hexdigest()
    body = {key: manifest[key] for key in manifest if key != "manifest_sha256"}
    manifest["manifest_sha256"] = hashlib.sha256(json.dumps(
        body, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode()).hexdigest()
    return manifest


def test_packaged_seed_manifest_is_candidate_only_and_within_confidence_ceiling():
    from odibi_anchor._dispatcher._memory_actions import _validate_seed_manifest
    from odibi_anchor.codebase._memory_db import CANDIDATE_CONFIDENCE_CEILING

    manifest = _repackaged_manifest()
    digest, entries = _validate_seed_manifest(manifest)
    assert digest == manifest["manifest_sha256"]
    assert entries
    assert {entry["status"] for entry in entries} == {"candidate"}
    assert all(entry["confidence"] <= CANDIDATE_CONFIDENCE_CEILING for entry in entries)
    assert all(entry["human_review"] is None for entry in entries)


def test_manifest_checksum_is_integrity_not_authenticity():
    """A self-consistently re-checksummed manifest still cannot create authority."""
    import hashlib

    from odibi_anchor._dispatcher._memory_actions import _validate_seed_manifest

    forged_status = _repackaged_manifest(status="confirmed")
    # The forged manifest is internally self-consistent: its carried checksum matches
    # its own edited content. That is precisely why the checksum cannot be treated as
    # authenticity -- it only proves the file was not corrupted in transit.
    body = {key: forged_status[key] for key in forged_status if key != "manifest_sha256"}
    assert forged_status["manifest_sha256"] == hashlib.sha256(json.dumps(
        body, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode()).hexdigest()
    # Every trust-bearing property is therefore independently rejected anyway.
    with pytest.raises(RuntimeError, match="invalid memory seed lifecycle or trust scope"):
        _validate_seed_manifest(forged_status)

    with pytest.raises(RuntimeError, match="invalid memory seed lifecycle or trust scope"):
        _validate_seed_manifest(_repackaged_manifest(human_review={"reviewer": "forged"}))

    with pytest.raises(RuntimeError, match="invalid seed confidence ceiling"):
        _validate_seed_manifest(_repackaged_manifest(confidence=0.95))

    with pytest.raises(RuntimeError, match="invalid memory seed lifecycle or trust scope"):
        _validate_seed_manifest(_repackaged_manifest(project="odibi_anchor"))

    with pytest.raises(RuntimeError, match="invalid seed evidence provenance"):
        _validate_seed_manifest(_repackaged_manifest(
            evidence={"trust_domain": "private", "repository": "x", "commit": "0" * 40,
                      "references": ["r"], "limitation": "l"},
        ))


@pytest.mark.parametrize("confidence", [0.5000001, 0.75, 1.0, float("nan"), float("inf"), True, "0.4", None])
def test_seed_confidence_ceiling_fails_closed(confidence):
    from odibi_anchor._dispatcher._memory_actions import _validate_seed_manifest

    with pytest.raises(RuntimeError, match="invalid seed confidence ceiling"):
        _validate_seed_manifest(_repackaged_manifest(confidence=confidence))


def test_loaded_seed_rows_never_exceed_confidence_ceiling(tmp_path):
    db = tmp_path / "memory.db"
    state = _state(tmp_path)
    from odibi_anchor.codebase._memory_db import CANDIDATE_CONFIDENCE_CEILING

    loaded = memory_action(
        tmp_path, ("seed",), {"command": "load", "db_path": str(db)},
        session_state=state, query_fn=None, render_fn=None,
    )
    assert isinstance(loaded, dict)
    with sqlite3.connect(db) as connection:
        rows = connection.execute(
            "SELECT status,confidence FROM memories WHERE source LIKE 'reviewed-seed:%'"
        ).fetchall()
    assert rows
    assert {row[0] for row in rows} == {"candidate"}
    assert max(row[1] for row in rows) <= CANDIDATE_CONFIDENCE_CEILING


# --- WI-2026-0019: append-only reviewed-seed confidence reconciliation --------

def _old_install(tmp_path, *, confidence=0.65, entry_index=0):
    """Seed a pre-upgrade install: one canonical seed row at the old ceiling."""
    import hashlib
    import pathlib

    from odibi_anchor._dispatcher import _memory_actions
    from odibi_anchor.codebase._memory_db import get_db

    db = tmp_path / "memory.db"
    resource = (
        pathlib.Path(_memory_actions.__file__).resolve().parent.parent
        / "seeds" / "agent_reliability.json"
    )
    manifest = json.loads(resource.read_text(encoding="utf-8"))
    entry = manifest["entries"][entry_index]
    evidence = json.dumps(dict(entry["evidence"]), sort_keys=True)
    connection = get_db(db)
    connection.execute(
        "INSERT INTO memories(id,project,type,content,related_files,tags,source,confidence,"
        "status,evidence,created,last_used) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (entry["id"], entry["project"], entry["type"], entry["content"],
         json.dumps(entry.get("related_files", [])), json.dumps(entry["tags"]),
         "reviewed-seed:" + entry["id"], confidence, "candidate", evidence,
         "2026-08-01T00:00:00+00:00", "2026-08-01T00:00:00+00:00"),
    )
    connection.commit()
    digest = hashlib.sha256(entry["content"].encode()).hexdigest()
    return db, entry, manifest["manifest_sha256"], digest


def _load_seeds(tmp_path, db):
    return memory_action(
        tmp_path, ("seed",), {"command": "load", "db_path": str(db)},
        session_state=_state(tmp_path), query_fn=None, render_fn=None,
    )


def test_old_install_upgrade_reconciles_without_mutating_the_superseded_row(tmp_path):
    """Regression for WI-2026-0019: the old ceiling used to dead-end seed load."""
    db, entry, manifest_sha, content_sha = _old_install(tmp_path)

    result = _load_seeds(tmp_path, db)
    record = next(item for item in result["loaded"] if item["id"] == entry["id"])
    assert record["action"] == "reconciled"
    assert record["prior_confidence"] == 0.65
    replacement_id = entry["id"] + ".v" + manifest_sha
    assert record["replacement_id"] == replacement_id

    connection = sqlite3.connect(db)
    connection.row_factory = sqlite3.Row
    try:
        old = connection.execute(
            "SELECT confidence,status,content FROM memories WHERE id=?", (entry["id"],)
        ).fetchone()
        new = connection.execute(
            "SELECT confidence,status,source,evidence FROM memories WHERE id=?", (replacement_id,)
        ).fetchone()
        event = connection.execute(
            "SELECT manifest_sha256,seed_id,replacement_id,prior_status,prior_confidence,"
            "prior_content_sha256 FROM memory_seed_reconciliation_events WHERE seed_id=?",
            (entry["id"],),
        ).fetchone()
    finally:
        connection.close()

    # The superseded row keeps its historical content/confidence; lifecycle state is withdrawn.
    assert old["confidence"] == 0.65
    assert old["status"] == "quarantined"
    assert old["content"] == entry["content"]
    # The replacement is a plain candidate at or below the ceiling.
    assert (new["confidence"], new["status"]) == (entry["confidence"], "candidate")
    assert new["confidence"] <= 0.5
    assert new["source"] == "reviewed-seed:" + replacement_id
    lineage = json.loads(new["evidence"])["supersedes"]
    assert lineage["prior_confidence"] == 0.65
    assert lineage["prior_status"] == "candidate"
    assert lineage["authority"].startswith("none")
    assert tuple(event) == (
        manifest_sha, entry["id"], replacement_id, "candidate", 0.65, content_sha,
    )


def test_seed_reconciliation_is_idempotent_across_retry_and_restart(tmp_path):
    db, entry, manifest_sha, _ = _old_install(tmp_path)
    from odibi_anchor.codebase._memory_db import close_db

    first = _load_seeds(tmp_path, db)
    second = _load_seeds(tmp_path, db)          # retry in the same process
    close_db(str(db))
    third = _load_seeds(tmp_path, db)           # restart: fresh connection

    for result in (first, second, third):
        record = next(item for item in result["loaded"] if item["id"] == entry["id"])
        assert record["action"] == "reconciled"
        assert record["replacement_id"] == entry["id"] + ".v" + manifest_sha

    connection = sqlite3.connect(db)
    try:
        assert connection.execute(
            "SELECT count(*) FROM memory_seed_reconciliation_events"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT count(*) FROM memories WHERE id LIKE ?", (entry["id"] + ".v%",)
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT count(*) FROM memory_seed_load_events"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT count(*) FROM memory_fts WHERE id LIKE ?", (entry["id"] + ".v%",)
        ).fetchone()[0] == 1
    finally:
        connection.close()


@pytest.mark.parametrize("tamper", ["memory", "fts"])
def test_seed_reconciliation_retry_rejects_tampered_replacement(tmp_path, tamper):
    """An immutable event cannot authorize a replacement that later drifted."""
    db, entry, manifest_sha, _ = _old_install(tmp_path)
    _load_seeds(tmp_path, db)
    replacement_id = entry["id"] + ".v" + manifest_sha

    connection = sqlite3.connect(db)
    try:
        if tamper == "memory":
            connection.execute(
                "UPDATE memories SET tags='[\"tampered\"]' WHERE id=?", (replacement_id,),
            )
        else:
            connection.execute("DELETE FROM memory_fts WHERE id=?", (replacement_id,))
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(RuntimeError, match="conflicting reviewed seed replacement"):
        _load_seeds(tmp_path, db)

    with sqlite3.connect(db) as connection:
        assert connection.execute(
            "SELECT count(*) FROM memory_seed_reconciliation_events WHERE seed_id=?",
            (entry["id"],),
        ).fetchone()[0] == 1


def test_seed_reconciliation_events_are_immutable(tmp_path):
    db, _entry, _sha, _digest = _old_install(tmp_path)
    _load_seeds(tmp_path, db)
    connection = sqlite3.connect(db)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("UPDATE memory_seed_reconciliation_events SET prior_confidence=0.1")
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("DELETE FROM memory_seed_reconciliation_events")
    finally:
        connection.close()


def test_seed_reconciliation_never_touches_user_memories(tmp_path):
    db, entry, _sha, _digest = _old_install(tmp_path)
    user = append_memory(
        tmp_path, entry_type="gotcha", content="A user-authored memory that must not move.",
        project="project:test", db_path=str(db),
    )
    twin = append_memory(
        tmp_path, entry_type=entry["type"], content=entry["content"],
        project="project:test", db_path=str(db),
    )
    query = (
        "SELECT id,project,type,content,confidence,status,source FROM memories "
        "WHERE source NOT LIKE 'reviewed-seed:%' ORDER BY id"
    )
    connection = sqlite3.connect(db)
    try:
        before = connection.execute(query).fetchall()
    finally:
        connection.close()

    _load_seeds(tmp_path, db)

    connection = sqlite3.connect(db)
    try:
        after = connection.execute(query).fetchall()
        touched = connection.execute(
            "SELECT count(*) FROM memory_seed_withdrawal_events WHERE seed_id IN (?,?)",
            (user["id"], twin["id"]),
        ).fetchone()[0]
    finally:
        connection.close()
    assert after == before
    assert touched == 0


def test_seed_reconciliation_fails_closed_against_a_competing_writer(tmp_path):
    db, entry, _sha, _digest = _old_install(tmp_path)
    from odibi_anchor.codebase._memory_db import close_db
    from odibi_anchor.codebase._sqlite_contention import PersistenceContentionError

    close_db(str(db))
    blocker = sqlite3.connect(db, timeout=0.1)
    blocker.execute("BEGIN IMMEDIATE")
    blocker.execute(
        "UPDATE memories SET last_used='2026-09-03T00:00:00+00:00' WHERE id=?", (entry["id"],)
    )
    try:
        with pytest.raises(PersistenceContentionError, match="store=shared_memory"):
            _load_seeds(tmp_path, db)
    finally:
        blocker.rollback()
        blocker.close()
        close_db(str(db))

    connection = sqlite3.connect(db)
    try:
        assert connection.execute(
            "SELECT count(*) FROM memories WHERE id LIKE ?", (entry["id"] + ".v%",)
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT confidence,status FROM memories WHERE id=?", (entry["id"],)
        ).fetchone() == (0.65, "candidate")
    finally:
        connection.close()

    record = next(
        item for item in _load_seeds(tmp_path, db)["loaded"] if item["id"] == entry["id"]
    )
    assert record["action"] == "reconciled"


def test_manifest_version_conflict_yields_a_distinct_replacement(tmp_path, monkeypatch):
    """A later manifest reconciles again without disturbing the earlier lineage."""
    import hashlib
    import pathlib

    db, entry, first_sha, _digest = _old_install(tmp_path)
    _load_seeds(tmp_path, db)

    from odibi_anchor._dispatcher import _memory_actions

    resource = (
        pathlib.Path(_memory_actions.__file__).resolve().parent.parent
        / "seeds" / "agent_reliability.json"
    )
    original_read_text = pathlib.Path.read_text

    def revised(self, *args, **kwargs):
        text = original_read_text(self, *args, **kwargs)
        if self != resource:
            return text
        manifest = json.loads(text)
        manifest["entries"][0]["confidence"] = 0.25
        for item in manifest["entries"]:
            body = {key: item[key] for key in item if key != "content_sha256"}
            item["content_sha256"] = hashlib.sha256(json.dumps(
                body, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
            ).encode()).hexdigest()
        body = {key: manifest[key] for key in manifest if key != "manifest_sha256"}
        manifest["manifest_sha256"] = hashlib.sha256(json.dumps(
            body, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ).encode()).hexdigest()
        return json.dumps(manifest)

    monkeypatch.setattr(pathlib.Path, "read_text", revised)
    second = _load_seeds(tmp_path, db)
    second_sha = second["manifest_sha256"]
    assert second_sha != first_sha

    record = next(item for item in second["loaded"] if item["id"] == entry["id"])
    assert record["action"] == "reconciled"
    assert record["replacement_id"] == entry["id"] + ".v" + second_sha

    connection = sqlite3.connect(db)
    try:
        events = connection.execute(
            "SELECT manifest_sha256,replacement_id FROM memory_seed_reconciliation_events "
            "WHERE seed_id=? ORDER BY manifest_sha256", (entry["id"],),
        ).fetchall()
        replacements = connection.execute(
            "SELECT id,confidence,status FROM memories WHERE id LIKE ? ORDER BY id",
            (entry["id"] + ".v%",),
        ).fetchall()
        canonical = connection.execute(
            "SELECT confidence,status FROM memories WHERE id=?", (entry["id"],)
        ).fetchone()
    finally:
        connection.close()

    assert len(events) == 2
    assert {row[0] for row in events} == {first_sha, second_sha}
    assert len(replacements) == 2
    assert all(row[2] == "candidate" and row[1] <= 0.5 for row in replacements)
    assert canonical == (0.65, "quarantined")


def test_seed_reconciliation_rejects_a_squatted_replacement_id(tmp_path):
    """A pre-existing row at the replacement id must fail closed, not be adopted."""
    from odibi_anchor.codebase._memory_db import get_db

    db, entry, manifest_sha, _digest = _old_install(tmp_path)
    replacement_id = entry["id"] + ".v" + manifest_sha
    connection = get_db(db)
    # Squat the replacement id with content that is NOT this manifest's entry.
    connection.execute(
        "INSERT INTO memories(id,project,type,content,related_files,tags,source,confidence,"
        "status,evidence,created,last_used) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (replacement_id, entry["project"], entry["type"], "squatted content",
         "[]", "[]", "reviewed-seed:" + replacement_id, 0.5, "candidate", "{}",
         "2026-08-02T00:00:00+00:00", "2026-08-02T00:00:00+00:00"),
    )
    connection.commit()

    with pytest.raises(RuntimeError, match="conflicting reviewed seed replacement"):
        _load_seeds(tmp_path, db)

    connection = sqlite3.connect(db)
    try:
        # Nothing was adopted, and no event claims the squatted row.
        assert connection.execute(
            "SELECT content FROM memories WHERE id=?", (replacement_id,)
        ).fetchone()[0] == "squatted content"
        reconciliation_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='memory_seed_reconciliation_events'"
        ).fetchone()
        if reconciliation_table:
            assert connection.execute(
                "SELECT count(*) FROM memory_seed_reconciliation_events"
            ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT status FROM memories WHERE id=?", (entry["id"],)
        ).fetchone()[0] == "candidate"
    finally:
        connection.close()
