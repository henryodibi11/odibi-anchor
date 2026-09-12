"""Fail-closed contracts for shadow memory-promotion evidence."""

from __future__ import annotations

import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

import odibi_anchor.codebase._memory_promotion as promotion_module
from odibi_anchor._dispatcher._memory_actions import memory_action
from odibi_anchor.codebase._memory_lifecycle import (
    evaluate_application,
    record_application,
    record_disposition_effects,
    record_selection,
)
from odibi_anchor.codebase._memory_lifecycle import (
    initialize_schema as initialize_memory_lifecycle,
)
from odibi_anchor.codebase._memory_promotion import (
    _HUMAN_RECEIPT_DDL,
    _V2_EVENT_DDL,
    POLICY_SHA256,
    POLICY_VERSION,
    SCHEMA_VERSION,
    V1_SCHEMA_SHA256,
    V2_SCHEMA_SHA256,
    V3_SCHEMA_SHA256,
    _append_event,
    _claim,
    _sha256,
    evaluate_shadow_promotion,
    initialize_schema,
    inspect_shadow_promotion,
    request_owner_promotion,
)
from odibi_anchor.codebase._task_execution import FORMAT, UNOBSERVED, persist_terminal_record
from odibi_anchor.codebase.memory_context import append_memory
from odibi_anchor.durability import ensure_database_authority


def _state(project: str | None = "project:test") -> SimpleNamespace:
    return SimpleNamespace(active_project=project, task_window_id="ltw_current")


def _owner_reply(monkeypatch, *, owner: str = "owner-1", response: str | None = None):
    requests = []
    transport = SimpleNamespace(name="slack")
    provider = SimpleNamespace(
        transport=transport,
        expected_owner_id="owner-1",
        assurance="remote_authenticated_slack_identity",
    )

    def approve(message, **_kwargs):
        requests.append(message)
        challenge = re.search(r"APPROVE ([0-9a-f]{64})", message).group(1)
        index = len(requests)
        return SimpleNamespace(
            request_id=f"request-{index}",
            response_message_id=f"reply-{index}",
            response=response or f"APPROVE {challenge}",
            response_user_id=owner,
            transport=transport.name,
            transport_ref="private-reference",
            message=message,
            created_at=float(index),
            delivered_at=float(index) + 0.1,
            responded_at=float(index) + 0.2,
        )

    monkeypatch.setattr(
        "odibi_anchor.human_input_owner.select_owner_approval_provider",
        lambda **_kwargs: provider,
    )
    monkeypatch.setattr("odibi_anchor.human_input.request_human_input_record", approve)
    return requests


def _drop_human_receipt_schema(connection: sqlite3.Connection) -> None:
    for statement in reversed(_HUMAN_RECEIPT_DDL):
        object_name = statement.split()[2]
        object_type = "TRIGGER" if statement.startswith("CREATE TRIGGER") else (
            "INDEX" if statement.startswith("CREATE INDEX") else "TABLE"
        )
        connection.execute(f"DROP {object_type} {object_name}")


def _terminal_record(db, *, task: str, project: str) -> dict:
    return persist_terminal_record(
        db,
        {
            "format": FORMAT,
            "identities": {
                "task_window_id": task,
                "session_id": f"session:{task}",
                "project_id": project,
                "problem_id": None,
                "spec_id": None,
                "work_item_id": None,
            },
            "started_at": "2026-09-01T00:00:00Z",
            "ended_at": "2026-09-01T00:01:00Z",
            "repository": {"start_revision": "a" * 40, "end_revision": "a" * 40},
            "terminal": {"status": "completed"},
            "coverage": {
                "unobserved": list(UNOBSERVED),
                "unavailable": [],
                "replay_claim": "none",
            },
        },
    )


def _helpful_nomination(db, *, memory_id: str, task: str, project: str) -> dict:
    selected = record_selection(
        db,
        task_window_id=task,
        query={"goal_present": True, "project": project},
        memory_id=memory_id,
        reason={"matched_query_terms": ["promotion", "evidence"]},
    )
    applied = record_application(
        db,
        selection_id=selected["selection_id"],
        task_window_id=task,
        action="use candidate guidance",
        context={"source_revision": "a" * 40},
    )
    evaluated = evaluate_application(
        db,
        application_id=applied["application_id"],
        task_window_id=task,
        outcome="helpful",
        evidence={"test": "focused test passed"},
    )
    terminal = _terminal_record(db, task=task, project=project)
    return {"evaluation": evaluated, "terminal": terminal}


def test_shadow_decision_retains_legacy_nomination_without_promotion(tmp_path):
    db = tmp_path / "memory.db"
    memory = append_memory(
        tmp_path,
        entry_type="gotcha",
        content="Promotion requires verifier-bound evidence for the exact claim.",
        project="project:test",
        db_path=str(db),
    )
    nomination = _helpful_nomination(
        db, memory_id=memory["id"], task="ltw_nomination", project="project:test",
    )
    with sqlite3.connect(db) as connection:
        before = connection.execute(
            "SELECT status,confidence,confirmation_count,last_confirmed FROM memories WHERE id=?",
            (memory["id"],),
        ).fetchone()

    first = evaluate_shadow_promotion(
        db, memory_id=memory["id"], project_id="project:test",
    )
    second = evaluate_shadow_promotion(
        db, memory_id=memory["id"], project_id="project:test",
    )

    assert first["decision_id"] == second["decision_id"]
    decision = first["decision"]
    assert decision["outcome"] == "rejected"
    assert decision["reason"] == "no_verifier_bound_attestations"
    assert decision["attestation_ids"] == []
    assert decision["authority_mutation"] == "none"
    assert decision["legacy_nominations"] == [
        {
            "evaluation_id": nomination["evaluation"]["evaluation_id"],
            "outcome": "helpful",
            "terminal_record_id": nomination["terminal"]["record_id"],
            "terminal_record_sha256": nomination["terminal"]["record_sha256"],
            "task_window_id": "ltw_nomination",
            "authority": "legacy_unbound_nomination",
        }
    ]
    assert len(decision["claim_sha256"]) == 64
    assert decision["policy_version"] == POLICY_VERSION
    assert decision["policy_sha256"] == POLICY_SHA256
    with sqlite3.connect(db) as connection:
        after = connection.execute(
            "SELECT status,confidence,confirmation_count,last_confirmed FROM memories WHERE id=?",
            (memory["id"],),
        ).fetchone()
        assert after == before == ("candidate", 0.5, 0, None)
        assert connection.execute(
            "SELECT count(*) FROM memory_promotion_decisions"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT count(*) FROM memory_promotion_attestations"
        ).fetchone()[0] == 0


def test_shadow_decision_is_concurrently_idempotent(tmp_path):
    db = tmp_path / "memory.db"
    memory = append_memory(
        tmp_path, entry_type="pattern", content="Concurrent shadow evaluation.",
        project="project:test", db_path=str(db),
    )

    def evaluate(_index):
        return evaluate_shadow_promotion(
            db, memory_id=memory["id"], project_id="project:test",
        )["decision_id"]

    with ThreadPoolExecutor(max_workers=4) as executor:
        decision_ids = list(executor.map(evaluate, range(4)))
    assert len(set(decision_ids)) == 1
    with sqlite3.connect(db) as connection:
        assert connection.execute(
            "SELECT count(*) FROM memory_promotion_decisions"
        ).fetchone()[0] == 1


def test_new_terminal_nomination_appends_a_new_shadow_decision(tmp_path):
    db = tmp_path / "memory.db"
    memory = append_memory(
        tmp_path, entry_type="gotcha", content="Evidence changes append decisions.",
        project="project:test", db_path=str(db),
    )
    first = evaluate_shadow_promotion(
        db, memory_id=memory["id"], project_id="project:test",
    )
    _helpful_nomination(
        db, memory_id=memory["id"], task="ltw_later", project="project:test",
    )
    second = evaluate_shadow_promotion(
        db, memory_id=memory["id"], project_id="project:test",
    )
    assert first["decision_id"] != second["decision_id"]
    assert second["decision"]["reason"] == "no_verifier_bound_attestations"
    assert len(second["decision"]["nomination_ids"]) == 1
    with sqlite3.connect(db) as connection:
        assert connection.execute(
            "SELECT count(*) FROM memory_promotion_decisions"
        ).fetchone()[0] == 2


def test_shadow_schema_and_events_are_exact_immutable_and_detect_drift(tmp_path):
    db = tmp_path / "memory.db"
    memory = append_memory(
        tmp_path, entry_type="pattern", content="Immutable shadow decision.",
        project="project:test", db_path=str(db),
    )
    evaluate_shadow_promotion(db, memory_id=memory["id"], project_id="project:test")
    with sqlite3.connect(db) as connection:
        connection.execute(
            "INSERT INTO memory_promotion_attestations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "attestation:test", memory["id"], "a" * 64, "project:test", "project:test",
                "terminal:test", "b" * 64, "c" * 64, "d" * 64, "e" * 64,
                "f" * 64, "verifier:test", "1", "derivation:test", "evidence:test",
                "clear", "{}", "2026-09-01T00:00:00Z",
            ),
        )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("UPDATE memory_promotion_decisions SET reason='rewritten'")
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("DELETE FROM memory_promotion_decisions")
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("UPDATE memory_promotion_attestations SET verifier_version='x'")
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("DELETE FROM memory_promotion_attestations")
        connection.execute("DROP INDEX idx_memory_promotion_decisions_memory")
    with pytest.raises(RuntimeError, match="schema checksum mismatch"):
        inspect_shadow_promotion(db, project_id="project:test")


def test_promotion_schema_v1_migrates_with_content_addressed_backup(tmp_path):
    db = tmp_path / "memory.db"
    memory = append_memory(
        tmp_path, entry_type="pattern", content="Promotion schema migration.",
        project="project:test", db_path=str(db),
    )
    evaluate_shadow_promotion(db, memory_id=memory["id"], project_id="project:test")
    with sqlite3.connect(db) as connection:
        _drop_human_receipt_schema(connection)
        connection.execute("DROP TRIGGER memory_promotion_events_no_delete")
        connection.execute("DROP TRIGGER memory_promotion_events_no_update")
        connection.execute("DROP INDEX idx_memory_promotion_events_memory")
        connection.execute("DROP TABLE memory_promotion_events")
        connection.execute(
            "UPDATE anchor_schema_versions SET version=1,schema_sha256=? WHERE domain='memory_promotion'",
            (V1_SCHEMA_SHA256,),
        )

    result = initialize_schema(db)
    retry = initialize_schema(db)

    assert result["version"] == retry["version"] == SCHEMA_VERSION
    backups = list(tmp_path.glob("memory.db.pre-memory-promotion-v4.*.bak"))
    assert len(backups) == 1
    with sqlite3.connect(db) as connection:
        assert connection.execute(
            "SELECT count(*) FROM memory_promotion_events"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT count(*) FROM memory_promotion_decisions"
        ).fetchone()[0] == 1


def test_promotion_schema_v2_migrates_without_rewriting_other_ledgers(tmp_path):
    db = tmp_path / "memory.db"
    memory = append_memory(
        tmp_path, entry_type="pattern", content="Promotion schema v2 migration.",
        project="project:test", db_path=str(db),
    )
    evaluate_shadow_promotion(db, memory_id=memory["id"], project_id="project:test")
    with sqlite3.connect(db) as connection:
        _drop_human_receipt_schema(connection)
        connection.row_factory = sqlite3.Row
        connection.execute("DROP TRIGGER memory_promotion_events_no_delete")
        connection.execute("DROP TRIGGER memory_promotion_events_no_update")
        connection.execute("DROP INDEX idx_memory_promotion_events_memory")
        connection.execute("DROP TABLE memory_promotion_events")
        for statement in _V2_EVENT_DDL:
            connection.execute(statement)
        connection.execute(
            "UPDATE anchor_schema_versions SET version=2,schema_sha256=? "
            "WHERE domain='memory_promotion'",
            (V2_SCHEMA_SHA256,),
        )
        row = connection.execute("SELECT * FROM memories WHERE id=?", (memory["id"],)).fetchone()
        activation, _ = _append_event(
            connection, memory_id=memory["id"], claim_sha256=_sha256(_claim(row)),
            project_id="project:test", event_type="activation", prior_event_id=None,
            target_status="active", attestation_ids=["attestation:legacy"],
        )
        _append_event(
            connection, memory_id=memory["id"], claim_sha256=_sha256(_claim(row)),
            project_id="project:test", event_type="withdrawal",
            prior_event_id=activation["event_id"], target_status="candidate",
            attestation_ids=["attestation:legacy"],
        )

    assert initialize_schema(db)["version"] == SCHEMA_VERSION
    assert len(list(tmp_path.glob("memory.db.pre-memory-promotion-v4.*.bak"))) == 1
    with sqlite3.connect(db) as connection:
        sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name='memory_promotion_events'"
        ).fetchone()[0]
        assert "confirmation" in sql
        assert connection.execute(
            "SELECT count(*) FROM memory_promotion_events"
        ).fetchone()[0] == 2
        assert connection.execute(
            "SELECT count(*) FROM memory_promotion_decisions"
        ).fetchone()[0] == 1
    assert inspect_shadow_promotion(
        db, project_id="project:test",
    )["effective_active_memory_ids"] == []


def test_promotion_schema_v3_adds_owner_receipts_without_rewriting_ledgers(tmp_path):
    db = tmp_path / "memory.db"
    memory = append_memory(
        tmp_path, entry_type="pattern", content="Promotion schema v3 migration.",
        project="project:test", db_path=str(db),
    )
    decision = evaluate_shadow_promotion(
        db, memory_id=memory["id"], project_id="project:test",
    )
    with sqlite3.connect(db) as connection:
        before = connection.execute(
            "SELECT decision_json FROM memory_promotion_decisions"
        ).fetchall()
        _drop_human_receipt_schema(connection)
        connection.execute(
            "UPDATE anchor_schema_versions SET version=3,schema_sha256=? "
            "WHERE domain='memory_promotion'",
            (V3_SCHEMA_SHA256,),
        )

    assert initialize_schema(db)["version"] == SCHEMA_VERSION
    assert len(list(tmp_path.glob("memory.db.pre-memory-promotion-v4.*.bak"))) == 1
    with sqlite3.connect(db) as connection:
        assert connection.execute(
            "SELECT decision_json FROM memory_promotion_decisions"
        ).fetchall() == before
        assert connection.execute(
            "SELECT count(*) FROM memory_human_authority_receipts"
        ).fetchone()[0] == 0
    assert decision["decision_id"] == inspect_shadow_promotion(
        db, project_id="project:test",
    )["decisions"][0]["decision_id"]


def test_authenticated_owner_activation_confirmation_and_replay_are_governed(
    tmp_path, monkeypatch,
):
    db = tmp_path / "memory.db"
    memory = append_memory(
        tmp_path, entry_type="convention", content="Use WHY-oriented review comments.",
        project="project:test", db_path=str(db),
    )
    requests = _owner_reply(monkeypatch)
    state = _state()

    activated = memory_action(
        tmp_path, ("promotion",),
        {
            "command": "request_owner_activation", "memory_id": memory["id"],
            "timeout_minutes": 1, "db_path": str(db),
        },
        session_state=state, query_fn=None, render_fn=None,
    )
    replay = request_owner_promotion(
        db, memory_id=memory["id"], project_id="project:test",
        transition="activation", timeout_minutes=1,
    )
    confirmed = memory_action(
        tmp_path, ("promotion",),
        {
            "command": "request_owner_confirmation", "memory_id": memory["id"],
            "timeout_minutes": 1, "db_path": str(db),
        },
        session_state=state, query_fn=None, render_fn=None,
    )

    assert activated["status"] == "recorded"
    assert activated["event"]["authority_lane"] == "human_owner"
    assert replay["status"] == "existing"
    assert replay["event"]["event_id"] == activated["event"]["event_id"]
    assert confirmed["status"] == "recorded"
    assert confirmed["receipt"]["prior_event_id"] == activated["event"]["event_id"]
    assert confirmed["event"]["authority_lane"] == "human_owner"
    assert len(requests) == 2
    assert all("Use WHY-oriented review comments." in message for message in requests)
    with sqlite3.connect(db) as connection:
        assert connection.execute(
            "SELECT status,confirmation_count FROM memories WHERE id=?", (memory["id"],),
        ).fetchone() == ("confirmed", 0)
        assert connection.execute(
            "SELECT count(*) FROM memory_human_authority_receipts"
        ).fetchone()[0] == 2
        assert connection.execute(
            "SELECT count(*) FROM memory_promotion_events"
        ).fetchone()[0] == 2
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("UPDATE memory_human_authority_receipts SET transport='forged'")
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("DELETE FROM memory_human_authority_receipts")
    diagnostics = inspect_shadow_promotion(db, project_id="project:test")
    assert diagnostics["counts"]["human_receipts"] == 2
    assert diagnostics["counts"]["authority_lanes"] == {"human_owner": 2}
    assert diagnostics["counts"]["effective_confirmed"] == 1


def test_correction_withdraws_owner_authority_and_only_owner_can_reactivate(
    tmp_path, monkeypatch,
):
    db = tmp_path / "memory.db"
    memory = append_memory(
        tmp_path, entry_type="convention", content="Use an obsolete review convention.",
        project="project:test", db_path=str(db),
    )
    requests = _owner_reply(monkeypatch)
    activated = request_owner_promotion(
        db, memory_id=memory["id"], project_id="project:test", transition="activation",
    )
    selection = record_selection(
        db, task_window_id="ltw-correction", query={"task": "review"},
        memory_id=memory["id"], reason={"match": "convention"},
    )
    record_disposition_effects(
        db, selection_id=selection["selection_id"], task_window_id="ltw-correction",
        disposition="suspect", reason={"evidence": "current source contradicts it"},
    )
    with sqlite3.connect(db) as connection:
        assert connection.execute(
            "SELECT status FROM memories WHERE id=?", (memory["id"],),
        ).fetchone()[0] == "quarantined"
        assert [row[0] for row in connection.execute(
            "SELECT event_type FROM memory_promotion_events ORDER BY created_at,event_id"
        )] == ["activation", "withdrawal"]

    reactivated = request_owner_promotion(
        db, memory_id=memory["id"], project_id="project:test", transition="activation",
    )
    assert reactivated["status"] == "recorded"
    assert reactivated["event"]["prior_event_id"] != activated["event"]["event_id"]
    assert len(requests) == 2
    with sqlite3.connect(db) as connection:
        assert connection.execute(
            "SELECT status FROM memories WHERE id=?", (memory["id"],),
        ).fetchone()[0] == "active"
        assert [row[0] for row in connection.execute(
            "SELECT event_type FROM memory_promotion_events ORDER BY created_at,event_id"
        )] == ["activation", "withdrawal", "activation"]


def test_owner_can_activate_candidate_correction_that_never_had_authority(tmp_path, monkeypatch):
    db = tmp_path / "memory.db"
    memory = append_memory(
        tmp_path, entry_type="convention", content="Corrected candidate convention.",
        project="project:test", db_path=str(db),
    )
    selection = record_selection(
        db, task_window_id="ltw-candidate-correction", query={"task": "review"},
        memory_id=memory["id"], reason={"match": "convention"},
    )
    record_disposition_effects(
        db, selection_id=selection["selection_id"],
        task_window_id="ltw-candidate-correction", disposition="suspect",
        reason={"evidence": "owner review is required"},
    )
    _owner_reply(monkeypatch)

    activated = request_owner_promotion(
        db, memory_id=memory["id"], project_id="project:test", transition="activation",
    )

    assert activated["status"] == "recorded"
    assert activated["event"]["prior_event_id"] is None
    with sqlite3.connect(db) as connection:
        assert connection.execute(
            "SELECT status FROM memories WHERE id=?", (memory["id"],),
        ).fetchone()[0] == "active"


def test_local_windows_owner_presence_activates_with_honest_receipt(tmp_path, monkeypatch):
    db = tmp_path / "memory.db"
    memory = append_memory(
        tmp_path, entry_type="convention", content="Owner-approved local convention.",
        project="project:test", db_path=str(db),
    )
    for name in ("ANCHOR_SLACK_BOT_TOKEN", "ANCHOR_SLACK_CHANNEL_ID", "ANCHOR_SLACK_USER_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ANCHOR_HUMAN_INPUT_STATE_PATH", str(tmp_path / "human-input.db"))
    monkeypatch.setattr("odibi_anchor.human_input_owner._is_windows", lambda: True)
    monkeypatch.setattr("odibi_anchor.human_input_windows._windows_username", lambda: "Henry")
    monkeypatch.setattr("odibi_anchor.human_input_windows._message_box", lambda *_args: 6)

    result = request_owner_promotion(
        db, memory_id=memory["id"], project_id="project:test",
        transition="activation", timeout_minutes=1,
    )

    assert result["status"] == "recorded"
    assert result["receipt"]["transport"] == "local-windows-owner-presence"
    assert result["receipt"]["owner_user_id"] == "windows-account:Henry"
    assert (
        result["receipt"]["owner_assurance"]
        == "interactive_local_windows_account_presence"
    )
    assert result["event"]["authority_lane"] == "human_owner"


def test_databricks_in_session_requires_two_explicit_separate_challenges(tmp_path, monkeypatch):
    db = tmp_path / "memory.db"
    memory = append_memory(
        tmp_path, entry_type="convention", content="Use the governed Databricks workflow.",
        project="project:test", db_path=str(db),
    )
    for name in ("ANCHOR_SLACK_BOT_TOKEN", "ANCHOR_SLACK_CHANNEL_ID", "ANCHOR_SLACK_USER_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DATABRICKS_RUNTIME_VERSION", "serverless")
    monkeypatch.setattr("odibi_anchor.human_input_owner._is_windows", lambda: False)
    monkeypatch.setenv("ANCHOR_HUMAN_INPUT_STATE_PATH", str(tmp_path / "human-input.db"))

    activation_request = request_owner_promotion(
        db, memory_id=memory["id"], project_id="project:test", transition="activation",
    )
    assert activation_request["status"] == "approval_required"
    assert activation_request["write_performed"] is False
    assert "No owner identity was authenticated" in activation_request["warning"]
    with sqlite3.connect(db) as connection:
        assert connection.execute(
            "SELECT status FROM memories WHERE id=?", (memory["id"],),
        ).fetchone()[0] == "candidate"
        assert connection.execute(
            "SELECT count(*) FROM memory_human_authority_receipts"
        ).fetchone()[0] == 0

    activated = request_owner_promotion(
        db, memory_id=memory["id"], project_id="project:test", transition="activation",
        in_session_approval=activation_request["approval_response"],
    )
    confirmation_request = request_owner_promotion(
        db, memory_id=memory["id"], project_id="project:test", transition="confirmation",
    )
    assert confirmation_request["status"] == "approval_required"
    assert confirmation_request["challenge_sha256"] != activation_request["challenge_sha256"]

    confirmed = request_owner_promotion(
        db, memory_id=memory["id"], project_id="project:test", transition="confirmation",
        in_session_approval=confirmation_request["approval_response"],
    )

    assert activated["receipt"]["owner_assurance"] == (
        "lower_assurance_single_user_databricks_in_session_assertion"
    )
    assert activated["receipt"]["transport"] == "databricks-in-session-owner-assertion"
    assert confirmed["receipt"]["prior_event_id"] == activated["event"]["event_id"]
    assert confirmed["receipt"]["challenge_sha256"] != activated["receipt"]["challenge_sha256"]
    with sqlite3.connect(db) as connection:
        assert connection.execute(
            "SELECT status FROM memories WHERE id=?", (memory["id"],),
        ).fetchone()[0] == "confirmed"
        assert connection.execute(
            "SELECT count(*) FROM memory_human_authority_receipts"
        ).fetchone()[0] == 2


def test_explicit_databricks_provider_prepares_with_complete_slack(tmp_path, monkeypatch):
    db = tmp_path / "memory.db"
    memory = append_memory(
        tmp_path, entry_type="convention", content="Use explicit Databricks approval.",
        project="project:test", db_path=str(db),
    )
    monkeypatch.setenv("ANCHOR_SLACK_BOT_TOKEN", "token")
    monkeypatch.setenv("ANCHOR_SLACK_CHANNEL_ID", "C123")
    monkeypatch.setenv("ANCHOR_SLACK_USER_ID", "U123")
    monkeypatch.setenv("DATABRICKS_RUNTIME_VERSION", "serverless")
    monkeypatch.setattr("odibi_anchor.human_input_owner._is_windows", lambda: False)
    monkeypatch.setenv("ANCHOR_HUMAN_INPUT_STATE_PATH", str(tmp_path / "human-input.db"))

    prepared = request_owner_promotion(
        db, memory_id=memory["id"], project_id="project:test", transition="activation",
        provider="databricks_in_session",
    )
    assert prepared["status"] == "approval_required"
    assert prepared["provider"] == "databricks_in_session"
    with sqlite3.connect(db) as connection:
        assert connection.execute(
            "SELECT status FROM memories WHERE id=?", (memory["id"],),
        ).fetchone()[0] == "candidate"
        assert connection.execute(
            "SELECT count(*) FROM memory_human_authority_receipts"
        ).fetchone()[0] == 0

    activated = request_owner_promotion(
        db, memory_id=memory["id"], project_id="project:test", transition="activation",
        provider="databricks_in_session", in_session_approval=prepared["approval_response"],
    )

    assert activated["status"] == "recorded"
    assert activated["receipt"]["transport"] == "databricks-in-session-owner-assertion"


def test_owner_promotes_shared_memory_only_with_matching_work_authority(tmp_path, monkeypatch):
    db = tmp_path / "memory.db"
    memory = append_memory(
        tmp_path, entry_type="convention", content="Use equivalence tests across projects.",
        project="all", db_path=str(db),
    )
    ensure_database_authority(
        db, authority_id="enterprise-analytics", trust_domain="work", initialize=True,
    )
    monkeypatch.setenv("DATABRICKS_RUNTIME_VERSION", "serverless")
    monkeypatch.setattr("odibi_anchor.human_input_owner._is_windows", lambda: False)
    monkeypatch.setenv("ANCHOR_HUMAN_INPUT_STATE_PATH", str(tmp_path / "human-input.db"))

    with pytest.raises(ValueError, match="boot-verified work authority"):
        request_owner_promotion(
            db, memory_id=memory["id"], project_id="project:one", transition="activation",
            provider="databricks_in_session",
        )
    with pytest.raises(RuntimeError, match="authority identity conflicts"):
        request_owner_promotion(
            db, memory_id=memory["id"], project_id="project:one", transition="activation",
            authority_id="another-authority", trust_domain="work",
            provider="databricks_in_session",
        )

    prepared = request_owner_promotion(
        db, memory_id=memory["id"], project_id="project:one", transition="activation",
        authority_id="enterprise-analytics", trust_domain="work",
        provider="databricks_in_session",
    )
    assert prepared["status"] == "approval_required"
    assert prepared["approval_prompt"].startswith("Odibi Anchor memory authority request.")

    activated = request_owner_promotion(
        db, memory_id=memory["id"], project_id="project:one", transition="activation",
        authority_id="enterprise-analytics", trust_domain="work",
        provider="databricks_in_session", in_session_approval=prepared["approval_response"],
    )
    assert activated["receipt"]["project_id"] == "all"
    assert activated["receipt"]["trust_domain"] == "work"
    assert activated["receipt"]["authority_id"] == "enterprise-analytics"
    assert activated["receipt"]["subject"]["active_project_id"] == "project:one"
    assert activated["event"]["project_id"] == "all"
    assert activated["event"]["trust_domain"] == "work"
    assert activated["event"]["authority_id"] == "enterprise-analytics"

    confirmation = request_owner_promotion(
        db, memory_id=memory["id"], project_id="project:two", transition="confirmation",
        authority_id="enterprise-analytics", trust_domain="work",
        provider="databricks_in_session",
    )
    confirmed = request_owner_promotion(
        db, memory_id=memory["id"], project_id="project:two", transition="confirmation",
        authority_id="enterprise-analytics", trust_domain="work",
        provider="databricks_in_session", in_session_approval=confirmation["approval_response"],
    )
    assert confirmed["receipt"]["subject"]["active_project_id"] == "project:two"
    assert confirmed["receipt"]["prior_event_id"] == activated["event"]["event_id"]
    withdrawn = promotion_module.withdraw_candidate_activation(
        db, memory_id=memory["id"], project_id="project:one",
        authority_id="enterprise-analytics", trust_domain="work",
    )
    assert withdrawn["event"]["project_id"] == "all"
    assert withdrawn["event"]["trust_domain"] == "work"
    assert withdrawn["event"]["authority_id"] == "enterprise-analytics"
    with sqlite3.connect(db) as connection:
        assert connection.execute(
            "SELECT status FROM memories WHERE id=?", (memory["id"],),
        ).fetchone()[0] == "candidate"


def test_databricks_in_session_stale_challenge_grants_no_authority(tmp_path, monkeypatch):
    db = tmp_path / "memory.db"
    memory = append_memory(
        tmp_path, entry_type="convention", content="Original Databricks claim.",
        project="project:test", db_path=str(db),
    )
    for name in ("ANCHOR_SLACK_BOT_TOKEN", "ANCHOR_SLACK_CHANNEL_ID", "ANCHOR_SLACK_USER_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DATABRICKS_RUNTIME_VERSION", "serverless")
    monkeypatch.setattr("odibi_anchor.human_input_owner._is_windows", lambda: False)
    monkeypatch.setenv("ANCHOR_HUMAN_INPUT_STATE_PATH", str(tmp_path / "human-input.db"))
    prepared = request_owner_promotion(
        db, memory_id=memory["id"], project_id="project:test", transition="activation",
    )
    with sqlite3.connect(db) as connection:
        connection.execute(
            "UPDATE memories SET content='Changed Databricks claim.' WHERE id=?", (memory["id"],),
        )

    import odibi_anchor.human_input as current_human_input

    with pytest.raises(
        current_human_input.HumanInputDeliveryError,
        match="does not match the current exact challenge",
    ):
        request_owner_promotion(
            db, memory_id=memory["id"], project_id="project:test", transition="activation",
            in_session_approval=prepared["approval_response"],
        )
    with sqlite3.connect(db) as connection:
        assert connection.execute(
            "SELECT status FROM memories WHERE id=?", (memory["id"],),
        ).fetchone()[0] == "candidate"
        assert connection.execute(
            "SELECT count(*) FROM memory_human_authority_receipts"
        ).fetchone()[0] == 0


@pytest.mark.parametrize(
    ("dialog_result", "error_type_name"),
    [(7, "ValueError"), (32_000, "HumanInputTimeout"), (0, "HumanInputDeliveryError")],
)
def test_local_owner_decline_timeout_or_invalid_ui_is_non_mutating(
    tmp_path, monkeypatch, dialog_result, error_type_name,
):
    db = tmp_path / "memory.db"
    memory = append_memory(
        tmp_path, entry_type="convention", content="No approval means no authority.",
        project="project:test", db_path=str(db),
    )
    for name in ("ANCHOR_SLACK_BOT_TOKEN", "ANCHOR_SLACK_CHANNEL_ID", "ANCHOR_SLACK_USER_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ANCHOR_HUMAN_INPUT_STATE_PATH", str(tmp_path / "human-input.db"))
    monkeypatch.setattr("odibi_anchor.human_input_owner._is_windows", lambda: True)
    monkeypatch.setattr("odibi_anchor.human_input_windows._windows_username", lambda: "Henry")
    monkeypatch.setattr("odibi_anchor.human_input_windows.time.time", lambda: 0.0)
    monkeypatch.setattr(
        "odibi_anchor.human_input_windows._message_box", lambda *_args: dialog_result,
    )

    import odibi_anchor.human_input as current_human_input

    error_type = (
        ValueError if error_type_name == "ValueError"
        else getattr(current_human_input, error_type_name)
    )
    with pytest.raises(error_type):
        request_owner_promotion(
            db, memory_id=memory["id"], project_id="project:test",
            transition="activation", timeout_minutes=0.001,
        )
    with sqlite3.connect(db) as connection:
        assert connection.execute(
            "SELECT status FROM memories WHERE id=?", (memory["id"],),
        ).fetchone()[0] == "candidate"
        assert connection.execute(
            "SELECT count(*) FROM memory_human_authority_receipts"
        ).fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM memory_promotion_events").fetchone()[0] == 0


@pytest.mark.parametrize(
    ("legacy_version", "legacy_digest"),
    [
        (
            "verified-memory-promotion-v6",
            "91fdd32dbf61f45cdb7325ead551a06e110fb4276c2e48d39ca684eafbc54e01",
        ),
        (
            "verified-memory-promotion-v7",
            "a9ab28ee06037dbf57ccda7760a60b7b6d8e5224f4d3ae5dbf7a058a1557d0bf",
        ),
    ],
)
def test_prior_owner_receipts_remain_readable_after_provider_policy_upgrade(
    tmp_path, monkeypatch, legacy_version, legacy_digest,
):
    db = tmp_path / "memory.db"
    memory = append_memory(
        tmp_path, entry_type="convention", content="Retain prior owner authority.",
        project="project:test", db_path=str(db),
    )
    current_version = promotion_module.POLICY_VERSION
    current_digest = promotion_module.POLICY_SHA256
    monkeypatch.setattr(promotion_module, "POLICY_VERSION", legacy_version)
    monkeypatch.setattr(promotion_module, "POLICY_SHA256", legacy_digest)
    _owner_reply(monkeypatch)
    recorded = request_owner_promotion(
        db, memory_id=memory["id"], project_id="project:test",
        transition="activation", timeout_minutes=1,
    )
    monkeypatch.setattr(promotion_module, "POLICY_VERSION", current_version)
    monkeypatch.setattr(promotion_module, "POLICY_SHA256", current_digest)

    inspected = inspect_shadow_promotion(db, project_id="project:test")

    assert inspected["human_receipts"][0]["receipt_id"] == recorded["receipt"]["receipt_id"]
    assert inspected["events"][0]["policy_version"] == legacy_version


@pytest.mark.parametrize(
    ("owner", "response"),
    [("other-owner", None), ("owner-1", "APPROVE wrong")],
)
def test_owner_promotion_rejects_wrong_identity_or_response_without_authority(
    tmp_path, monkeypatch, owner, response,
):
    db = tmp_path / "memory.db"
    memory = append_memory(
        tmp_path, entry_type="convention", content="Authenticated owner only.",
        project="project:test", db_path=str(db),
    )
    _owner_reply(monkeypatch, owner=owner, response=response)

    with pytest.raises(ValueError, match="response text, identity, or provider differs"):
        request_owner_promotion(
            db, memory_id=memory["id"], project_id="project:test",
            transition="activation", timeout_minutes=1,
        )
    with sqlite3.connect(db) as connection:
        assert connection.execute(
            "SELECT status FROM memories WHERE id=?", (memory["id"],),
        ).fetchone()[0] == "candidate"
        assert connection.execute(
            "SELECT count(*) FROM memory_human_authority_receipts"
        ).fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM memory_promotion_events").fetchone()[0] == 0


def test_owner_promotion_without_available_provider_is_non_mutating(tmp_path, monkeypatch):
    from odibi_anchor.human_input import HumanInputConfigurationError

    db = tmp_path / "memory.db"
    memory = append_memory(
        tmp_path, entry_type="convention", content="No implicit owner authority.",
        project="project:test", db_path=str(db),
    )
    def unavailable(**_kwargs):
        raise HumanInputConfigurationError("no owner-presence provider")

    monkeypatch.setattr(
        "odibi_anchor.human_input_owner.select_owner_approval_provider", unavailable,
    )

    with pytest.raises(HumanInputConfigurationError, match="no owner-presence provider"):
        request_owner_promotion(
            db, memory_id=memory["id"], project_id="project:test",
            transition="activation", timeout_minutes=1,
        )
    with sqlite3.connect(db) as connection:
        assert connection.execute(
            "SELECT status FROM memories WHERE id=?", (memory["id"],),
        ).fetchone()[0] == "candidate"
        assert connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE name IN "
            "('memory_human_authority_receipts','memory_promotion_events')"
        ).fetchone()[0] == 0


def test_owner_promotion_revalidates_claim_after_authenticated_reply(tmp_path, monkeypatch):
    db = tmp_path / "memory.db"
    memory = append_memory(
        tmp_path, entry_type="convention", content="Original exact claim.",
        project="project:test", db_path=str(db),
    )
    provider = SimpleNamespace(
        transport=SimpleNamespace(name="slack"),
        expected_owner_id="owner-1",
        assurance="remote_authenticated_slack_identity",
    )
    monkeypatch.setattr(
        "odibi_anchor.human_input_owner.select_owner_approval_provider",
        lambda **_kwargs: provider,
    )

    def approve(message, **_kwargs):
        challenge = re.search(r"APPROVE ([0-9a-f]{64})", message).group(1)
        with sqlite3.connect(db) as connection:
            connection.execute(
                "UPDATE memories SET content='Changed after challenge' WHERE id=?", (memory["id"],),
            )
        return SimpleNamespace(
            request_id="request-stale", response_message_id="reply-stale",
            response=f"APPROVE {challenge}", response_user_id="owner-1",
            transport="slack", transport_ref="private-reference",
            message=message, created_at=1.0, delivered_at=1.1, responded_at=1.2,
        )

    monkeypatch.setattr("odibi_anchor.human_input.request_human_input_record", approve)
    with pytest.raises(ValueError, match="changed after owner approval"):
        request_owner_promotion(
            db, memory_id=memory["id"], project_id="project:test",
            transition="activation", timeout_minutes=1,
        )
    with sqlite3.connect(db) as connection:
        assert connection.execute(
            "SELECT status FROM memories WHERE id=?", (memory["id"],),
        ).fetchone()[0] == "candidate"
        assert connection.execute(
            "SELECT count(*) FROM memory_human_authority_receipts"
        ).fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM memory_promotion_events").fetchone()[0] == 0


def test_owner_diagnostics_reject_indexed_receipt_drift(tmp_path, monkeypatch):
    db = tmp_path / "memory.db"
    memory = append_memory(
        tmp_path, entry_type="convention", content="Receipt indexes must match payload.",
        project="project:test", db_path=str(db),
    )
    _owner_reply(monkeypatch)
    request_owner_promotion(
        db, memory_id=memory["id"], project_id="project:test",
        transition="activation", timeout_minutes=1,
    )
    with sqlite3.connect(db) as connection:
        trigger_sql = connection.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE name='memory_human_authority_receipts_no_update'"
        ).fetchone()[0]
        connection.execute("DROP TRIGGER memory_human_authority_receipts_no_update")
        connection.execute(
            "UPDATE memory_human_authority_receipts SET owner_user_id='forged-owner'"
        )
        connection.execute(trigger_sql)
    with pytest.raises(RuntimeError, match="indexed column mismatch"):
        inspect_shadow_promotion(db, project_id="project:test")


def test_shadow_diagnostics_reject_indexed_decision_drift(tmp_path):
    db = tmp_path / "memory.db"
    memory = append_memory(
        tmp_path, entry_type="pattern", content="Exact shadow diagnostics.",
        project="project:test", db_path=str(db),
    )
    evaluate_shadow_promotion(db, memory_id=memory["id"], project_id="project:test")
    with sqlite3.connect(db) as connection:
        trigger_sql = connection.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE name='memory_promotion_decisions_no_update'"
        ).fetchone()[0]
        connection.execute("DROP TRIGGER memory_promotion_decisions_no_update")
        connection.execute("UPDATE memory_promotion_decisions SET outcome='unavailable'")
        connection.execute(trigger_sql)
    with pytest.raises(RuntimeError, match="indexed column mismatch"):
        inspect_shadow_promotion(db, project_id="project:test")


def test_shadow_kill_switch_is_non_mutating(tmp_path, monkeypatch):
    db = tmp_path / "memory.db"
    memory = append_memory(
        tmp_path, entry_type="pattern", content="Disabled shadow evaluation.",
        project="project:test", db_path=str(db),
    )
    monkeypatch.setenv("ANCHOR_MEMORY_PROMOTION_SHADOW", "off")
    result = evaluate_shadow_promotion(
        db, memory_id=memory["id"], project_id="project:test",
    )
    assert result == {
        "kind": "memory_promotion_shadow",
        "status": "disabled",
        "reason": "shadow_kill_switch",
        "policy_version": POLICY_VERSION,
        "policy_sha256": POLICY_SHA256,
        "write_performed": False,
    }
    with sqlite3.connect(db) as connection:
        assert connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE name='memory_promotion_decisions'"
        ).fetchone()[0] == 0


def test_dispatcher_enforces_scope_and_rejects_caller_authored_authority(tmp_path):
    db = tmp_path / "memory.db"
    memory = append_memory(
        tmp_path, entry_type="gotcha", content="Project-local candidate.",
        project="private:other", db_path=str(db),
    )
    state = _state()
    with pytest.raises(ValueError, match="project/trust boundary"):
        memory_action(
            tmp_path,
            ("promotion",),
            {"command": "evaluate", "memory_id": memory["id"], "db_path": str(db)},
            session_state=state,
            query_fn=None,
            render_fn=None,
        )
    with pytest.raises(ValueError, match="unknown memory promotion arguments"):
        memory_action(
            tmp_path,
            ("promotion",),
            {
                "command": "evaluate",
                "memory_id": memory["id"],
                "attestations": [{"actor": "caller"}],
                "db_path": str(db),
            },
            session_state=state,
            query_fn=None,
            render_fn=None,
        )
    with sqlite3.connect(db) as connection:
        assert connection.execute(
            "SELECT status FROM memories WHERE id=?", (memory["id"],),
        ).fetchone()[0] == "candidate"


def test_dispatcher_inspection_and_diagnostics_report_exact_shadow_counts(tmp_path, monkeypatch):
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
    memory = append_memory(
        tmp_path, entry_type="decision", content="Shadow promotion remains advisory.",
        project="project:test", db_path=str(db),
    )
    state = _state()
    recorded = memory_action(
        tmp_path,
        ("promotion",),
        {"command": "evaluate", "memory_id": memory["id"], "db_path": str(db)},
        session_state=state,
        query_fn=None,
        render_fn=None,
    )
    inspected = memory_action(
        tmp_path,
        ("promotion",),
        {"command": "inspect", "memory_id": memory["id"], "db_path": str(db)},
        session_state=state,
        query_fn=None,
        render_fn=None,
    )
    initialize_memory_lifecycle(db)
    diagnostics = memory_action(
        tmp_path,
        ("diagnostics",),
        {"db_path": str(db)},
        session_state=state,
        query_fn=None,
        render_fn=None,
    )
    assert recorded["status"] == "recorded"
    assert inspected["counts"] == {
        "attestations": 0, "human_receipts": 0, "decisions": 1,
        "outcomes": {"rejected": 1}, "events": 0, "event_types": {},
        "authority_lanes": {}, "effective_active": 0, "effective_confirmed": 0,
    }
    assert inspected["decisions"][0]["decision_id"] == recorded["decision_id"]
    assert diagnostics["counts"]["durable_facts"]["promotion_events"] == 0
    assert diagnostics["counts"]["durable_facts"]["promotion_shadow_decisions"] == 1
    assert diagnostics["counts"]["durable_facts"]["promotion_attestations"] == 0
    assert diagnostics["counts"]["durable_facts"]["promotion_human_receipts"] == 0
    assert diagnostics["promotion_capability"] == {
        "status": "machine_and_owner_promotion_enabled",
        "reason": "verifier_attestations_or_governed_owner_receipts",
        "policy_version": POLICY_VERSION,
        "policy_sha256": POLICY_SHA256,
        "automatic_candidate_activation_enabled": True,
        "automatic_active_confirmation_enabled": True,
        "authenticated_human_provider_available": False,
        "authenticated_human_provider_configured": False,
        "human_owner_provider_available": True,
        "human_owner_provider_configured": True,
        "human_owner_provider_transport": "local-windows-owner-presence",
        "human_owner_provider_assurance": "interactive_local_windows_account_presence",
    }
