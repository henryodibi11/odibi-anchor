"""Focused contracts for the additive memory lifecycle domain."""

import hashlib
import json
import sqlite3

import pytest

from odibi_anchor.codebase._memory_lifecycle import (
    DOMAIN,
    SCHEMA_SHA256,
    V1_SCHEMA_SHA256,
    V2_SCHEMA_SHA256,
    RecoveryUnavailable,
    evaluate_application,
    evaluate_application_effects,
    initialize_schema,
    pending_task_selections,
    record_application,
    record_disposition,
    record_disposition_effects,
    record_projection,
    record_selection,
    rollback_recovery_schema,
    selection_recovery,
    task_memory_activity,
    task_memory_records,
    unevaluated_task_applications,
)


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "memory.db"
    connection = sqlite3.connect(path)
    connection.executescript("""
        CREATE TABLE memories (id TEXT PRIMARY KEY, project TEXT);
        CREATE TABLE learning_items (item_id TEXT PRIMARY KEY);
        INSERT INTO memories VALUES ('m1', 'project-a'), ('m2', 'project-a');
        INSERT INTO learning_items VALUES ('lesson1');
    """)
    connection.close()
    return path


def downgrade_fixture_to_v1(path):
    with sqlite3.connect(path) as connection:
        for kind, name in connection.execute(
            "SELECT type,name FROM sqlite_master WHERE "
            "name LIKE 'memory_selection_abandonments%' OR "
            "name LIKE 'memory_selection_recoveries%' OR "
            "name LIKE 'idx_memory_abandonments%' OR "
            "name LIKE 'idx_memory_recoveries%' OR "
            "name LIKE 'memory_lifecycle_migrations%' "
            "ORDER BY CASE type WHEN 'trigger' THEN 0 WHEN 'index' THEN 1 ELSE 2 END"
        ).fetchall():
            if kind == "trigger":
                connection.execute(f"DROP TRIGGER {name}")
            elif kind == "index":
                connection.execute(f"DROP INDEX {name}")
            elif kind == "table":
                connection.execute(f"DROP TABLE {name}")
        connection.execute("DROP INDEX idx_memory_dispositions_task")
        connection.execute("DROP TABLE memory_dispositions")
        connection.execute(
            "UPDATE anchor_schema_versions SET version=1,schema_sha256=? WHERE domain=?",
            (V1_SCHEMA_SHA256, DOMAIN),
        )


def downgrade_fixture_to_v2(path):
    with sqlite3.connect(path) as connection:
        for kind, name in connection.execute(
            "SELECT type,name FROM sqlite_master WHERE "
            "name LIKE 'memory_selection_abandonments%' OR "
            "name LIKE 'memory_selection_recoveries%' OR "
            "name LIKE 'idx_memory_abandonments%' OR "
            "name LIKE 'idx_memory_recoveries%' OR "
            "name LIKE 'memory_lifecycle_migrations%' "
            "ORDER BY CASE type WHEN 'trigger' THEN 0 WHEN 'index' THEN 1 ELSE 2 END"
        ).fetchall():
            connection.execute(f"DROP {kind.upper()} {name}")
        connection.execute(
            "UPDATE anchor_schema_versions SET version=2,schema_sha256=? WHERE domain=?",
            (V2_SCHEMA_SHA256, DOMAIN),
        )


def accepted_task(path, task, *, status=None, project="project-a", trust="private"):
    from odibi_anchor.codebase._task_authority import (
        close_accepted_task,
    )
    from odibi_anchor.codebase._task_authority import (
        initialize_schema as initialize_task_authority,
    )

    initialize_task_authority(path)
    root = path.parent
    accepted_at = "2026-09-10T00:00:00.000000Z"
    record_id = "atr_" + hashlib.sha256(task.encode()).hexdigest()
    record = {
        "format": "odibi-anchor-accepted-task-v1",
        "accepted_at": accepted_at,
        "record_id": record_id,
        "identity": {
            "task_window_id": task, "session_id": "session-" + task,
            "project_id": project, "anchor_home": str((root / "anchor").resolve()),
            "project_root": str((root / "artifacts").resolve()),
            "artifact_root": str((root / "artifacts").resolve()),
            "target_root": str((root / "target").resolve()),
            "repository_provider_id": None, "trust_domain": trust,
        },
        "task": {"profile": {"execution_mode": "source_change"}},
        "obligations": {"acceptance_criteria": [], "required_skills": []},
    }
    raw = json.dumps(record, sort_keys=True, separators=(",", ":"))
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO accepted_task_records VALUES(?,?,?,?,?,?,?,?,?)",
            (task, record_id, project, record["identity"]["target_root"], "source_change",
             accepted_at, raw, hashlib.sha256(raw.encode()).hexdigest(), accepted_at),
        )
    if status:
        close_accepted_task(path, task_window_id=task, terminal_status=status)
    return record["identity"]


def recovery_args(path, current="task-current", **changes):
    values = {
        "current_task_window_id": current,
        "project_id": "project-a",
        "anchor_home": str((path.parent / "anchor").resolve()),
        "project_root": str((path.parent / "artifacts").resolve()),
        "target_root": str((path.parent / "target").resolve()),
        "artifact_root": str((path.parent / "artifacts").resolve()),
        "repository_provider_id": None,
        "trust_domain": "private",
    }
    values.update(changes)
    return values


def test_schema_is_additive_idempotent_and_checksum_verified(db):
    assert initialize_schema(db) == initialize_schema(db)
    connection = sqlite3.connect(db)
    assert connection.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 2
    assert connection.execute(
        "SELECT schema_sha256 FROM anchor_schema_versions WHERE domain=?", (DOMAIN,)
    ).fetchone()[0] == SCHEMA_SHA256
    connection.execute("UPDATE anchor_schema_versions SET schema_sha256=? WHERE domain=?", ("0" * 64, DOMAIN))
    connection.commit()
    connection.close()
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        initialize_schema(db)


def test_schema_verification_detects_object_drift(db):
    initialize_schema(db)
    connection = sqlite3.connect(db)
    connection.execute("DROP INDEX idx_memory_selections_task")
    connection.commit()
    connection.close()
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        initialize_schema(db)


def test_v1_schema_migrates_additively_to_dispositions(db):
    initialize_schema(db)
    downgrade_fixture_to_v1(db)
    assert initialize_schema(db)["version"] == 3
    assert len(sorted(db.parent.glob(db.name + ".pre-memory-lifecycle-v2.*.bak"))) == 1
    assert len(sorted(db.parent.glob(db.name + ".pre-memory-lifecycle-v3.*.bak"))) == 1
    with sqlite3.connect(db) as connection:
        assert connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE name='memory_dispositions'"
        ).fetchone()[0] == 1


def test_v2_schema_migrates_to_v3_with_verified_marker_and_rerun(db):
    initialize_schema(db)
    downgrade_fixture_to_v2(db)

    first = initialize_schema(db)
    second = initialize_schema(db)

    assert first == second
    with sqlite3.connect(db) as connection:
        marker = connection.execute(
            "SELECT from_version,to_version,pre_v3_digest FROM memory_lifecycle_migrations"
        ).fetchone()
        assert marker[:2] == (2, 3)
        assert len(marker[2]) == 64
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_v3_rollback_is_allowed_only_before_recovery_history(db):
    initialize_schema(db)
    downgrade_fixture_to_v2(db)
    initialize_schema(db)

    assert rollback_recovery_schema(db)["version"] == 2
    assert initialize_schema(db)["version"] == 3


@pytest.mark.parametrize("drift", ["table", "index"])
def test_v1_migration_rejects_drift_before_backup_or_ddl(db, drift):
    initialize_schema(db)
    downgrade_fixture_to_v1(db)
    with sqlite3.connect(db) as connection:
        if drift == "table":
            connection.execute("ALTER TABLE memory_applications ADD COLUMN drift TEXT")
        else:
            connection.execute("DROP INDEX idx_memory_applications_task")
            connection.execute("CREATE INDEX idx_memory_applications_task ON memory_applications(action)")
    with pytest.raises(RuntimeError, match="schema checksum mismatch"):
        initialize_schema(db)
    assert not sorted(db.parent.glob(db.name + ".pre-memory-lifecycle-v2.*.bak"))
    with sqlite3.connect(db) as connection:
        assert connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE name='memory_dispositions'"
        ).fetchone()[0] == 0


def test_selection_is_stable_and_does_not_imply_application(db):
    first = record_selection(
        db, task_window_id="task-1", query={"goal": "ship", "tags": ["b", "a"]},
        memory_id="m1", reason={"match": "goal"},
    )
    second = record_selection(
        db, task_window_id="task-1", query={"tags": ["b", "a"], "goal": "ship"},
        memory_id="m1", reason={"match": "goal"},
    )
    assert first == second
    connection = sqlite3.connect(db)
    assert connection.execute("SELECT COUNT(*) FROM memory_selections").fetchone()[0] == 1
    assert connection.execute("SELECT COUNT(*) FROM memory_applications").fetchone()[0] == 0


def test_selection_batch_rolls_back_on_failure(db):
    from odibi_anchor.codebase._memory_lifecycle import record_selections

    with pytest.raises(sqlite3.IntegrityError):
        record_selections(
            db, task_window_id="task-1", query={"goal_sha256": "a" * 64},
            selections=[
                {"memory_id": "m1", "reason": {"match": "goal"}},
                {"memory_id": "missing", "reason": {"match": "goal"}},
            ],
        )
    connection = sqlite3.connect(db)
    assert connection.execute("SELECT COUNT(*) FROM memory_selections").fetchone()[0] == 0


def test_application_is_task_bound_and_evaluation_requires_evidence(db):
    selected = record_selection(
        db, task_window_id="task-1", query={"goal": "ship"}, memory_id="m1",
        reason={"match": "goal"},
    )
    with pytest.raises(ValueError, match="different task"):
        record_application(db, selection_id=selected["selection_id"], task_window_id="task-2", action="edit")
    applied = record_application(
        db, selection_id=selected["selection_id"], task_window_id="task-1", action="edit",
        context={"files": ["src/a.py"]},
    )
    assert unevaluated_task_applications(db, task_window_id="task-1") == [applied]
    assert unevaluated_task_applications(db, task_window_id="task-2") == []
    assert applied == record_application(
        db, selection_id=selected["selection_id"], task_window_id="task-1", action="edit",
        context={"files": ["src/a.py"]},
    )
    for outcome in ("helpful", "not_helpful", "harmful", "superseded"):
        target = applied if outcome == "helpful" else record_application(
            db, selection_id=selected["selection_id"], task_window_id="task-1", action=outcome
        )
        assert evaluate_application(
            db, application_id=target["application_id"], task_window_id="task-1",
            outcome=outcome, evidence={"test": outcome}
        )["outcome"] == outcome
    assert unevaluated_task_applications(db, task_window_id="task-1") == []
    with pytest.raises(ValueError, match="different task"):
        evaluate_application(
            db, application_id=applied["application_id"], task_window_id="task-2",
            outcome="helpful", evidence={"test": "passed"},
        )
    with pytest.raises(ValueError, match="non-empty"):
        evaluate_application(
            db, application_id=applied["application_id"], task_window_id="task-1",
            outcome="helpful", evidence={},
        )


def test_selection_requires_one_explicit_disposition_and_records_details(db):
    selected = record_selection(
        db, task_window_id="task-1", query={"goal": "ship"}, memory_id="m1",
        reason={"match": "goal"},
    )
    assert pending_task_selections(db, task_window_id="task-1") == [selected]
    disposition = record_disposition(
        db, selection_id=selected["selection_id"], task_window_id="task-1",
        disposition="irrelevant", reason={"basis": "wrong runtime"},
    )
    assert pending_task_selections(db, task_window_id="task-1") == []
    assert task_memory_records(db, task_window_id="task-1")["dispositions"] == [disposition]
    with pytest.raises(ValueError, match="conflicting memory disposition"):
        record_disposition(
            db, selection_id=selected["selection_id"], task_window_id="task-1",
            disposition="suspect", reason={"basis": "stale"},
        )
    with pytest.raises(ValueError, match="contradicts"):
        record_application(
            db, selection_id=selected["selection_id"], task_window_id="task-1", action="late apply",
        )
    with pytest.raises(ValueError, match="requires replacement"):
        record_disposition(
            db, selection_id=record_selection(
                db, task_window_id="task-2", query={"goal": "ship"}, memory_id="m2",
                reason={"match": "goal"},
            )["selection_id"],
            task_window_id="task-2", disposition="superseded", reason={"basis": "newer"},
        )


def test_abandonment_recovery_is_authoritative_idempotent_and_actionable(db):
    accepted_task(db, "task-origin", status="failed")
    accepted_task(db, "task-current")
    selected = record_selection(
        db, task_window_id="task-origin", query={"goal": "recover"},
        memory_id="m1", reason={"match": "goal"},
    )
    common = recovery_args(db)
    request = {
        **common, "selection_id": selected["selection_id"],
        "prior_task_window_id": "task-origin", "reason": {"basis": "failed task"},
        "evidence": {"terminal": "failed"},
    }

    abandoned = selection_recovery(db, action="declare_abandoned", **request)
    assert selection_recovery(db, action="declare_abandoned", **request) == abandoned
    assert abandoned["closure_status"] == "failed"
    with pytest.raises(ValueError, match="abandonment idempotency conflict"):
        selection_recovery(
            db, action="declare_abandoned",
            **{**request, "reason": {"basis": "changed replay"}},
        )
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT count(*) FROM memory_dispositions").fetchone()[0] == 0
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE memory_selection_abandonments SET actor_ref='other'"
            )

    recovered = selection_recovery(
        db, action="recover", **{**request, "reason": {"basis": "continue"}},
    )
    assert selection_recovery(
        db, action="recover", **{**request, "reason": {"basis": "continue"}},
    ) == recovered
    with pytest.raises(ValueError, match="recovery idempotency conflict"):
        selection_recovery(
            db, action="recover", **{**request, "reason": {"basis": "changed replay"}},
        )
    assert recovered["replacement_task_window_id"] == "task-current"
    assert pending_task_selections(db, task_window_id="task-origin") == []
    assert pending_task_selections(db, task_window_id="task-current") == [selected]
    assert task_memory_records(db, task_window_id="task-current")["selections"] == [selected]
    disposition = record_disposition(
        db, selection_id=selected["selection_id"], task_window_id="task-current",
        disposition="irrelevant", reason={"basis": "reviewed"},
    )
    assert disposition["task_window_id"] == "task-current"
    inspected = selection_recovery(db, action="inspect", **common)
    assert inspected["counts"]["semantically_disposed"] == 1
    with pytest.raises(RuntimeError, match="rollback refused"):
        rollback_recovery_schema(db)


def test_recovery_diagnostics_distinguish_all_operational_states(db):
    with sqlite3.connect(db) as connection:
        connection.executemany(
            "INSERT INTO memories VALUES (?, 'project-a')",
            [(f"m{number}",) for number in range(3, 7)],
        )
    for task, status in (
        ("task-current", None),
        ("task-terminal", "blocked"),
        ("task-abandoned", "failed"),
        ("task-recovered", "superseded"),
        ("task-disposed", None),
    ):
        accepted_task(db, task, status=status)
    selections = {
        state: record_selection(
            db, task_window_id=task, query={"state": state}, memory_id=f"m{index}",
            reason={"match": state},
        )
        for index, (state, task) in enumerate((
            ("active_pending", "task-current"),
            ("terminal_unresolved", "task-terminal"),
            ("abandoned", "task-abandoned"),
            ("recovered", "task-recovered"),
            ("semantically_disposed", "task-disposed"),
            ("ambiguous", "task-missing-authority"),
        ), start=1)
    }
    common = recovery_args(db)
    for state, task in (("abandoned", "task-abandoned"), ("recovered", "task-recovered")):
        selected = selections[state]
        request = {
            **common, "selection_id": selected["selection_id"],
            "prior_task_window_id": task, "reason": {"basis": state},
            "evidence": {"task_status": state},
        }
        selection_recovery(db, action="declare_abandoned", **request)
        if state == "recovered":
            selection_recovery(db, action="recover", **request)
    record_disposition(
        db, selection_id=selections["semantically_disposed"]["selection_id"],
        task_window_id="task-disposed", disposition="irrelevant",
        reason={"basis": "reviewed"},
    )

    inspected = selection_recovery(db, action="inspect", **common)

    assert inspected["counts"] == {
        "active_pending": 1,
        "terminal_unresolved": 1,
        "abandoned": 1,
        "recovered": 1,
        "semantically_disposed": 1,
        "ambiguous": 1,
    }
    assert {item["state"] for item in inspected["states"]} == set(inspected["counts"])


def test_recovery_rejects_fabricated_abandonment_authority(db):
    accepted_task(db, "task-origin", status="failed")
    accepted_task(db, "task-current")
    selected = record_selection(
        db, task_window_id="task-origin", query={"goal": "recover"},
        memory_id="m1", reason={"match": "goal"},
    )
    with sqlite3.connect(db) as connection:
        connection.execute(
            "INSERT INTO memory_selection_abandonments VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "msa-fabricated", selected["selection_id"], "task-origin", "project-a",
                "private", "0" * 64, "1" * 64, "ate-fabricated", "failed", "2" * 64,
                '{"basis":"fabricated"}', '{"claim":"failed"}', "agent", "test",
                "2026-09-10T00:00:00.000000Z",
            ),
        )

    with pytest.raises(RecoveryUnavailable, match="current exact owner"):
        selection_recovery(
            db, action="recover", **recovery_args(db),
            selection_id=selected["selection_id"], prior_task_window_id="task-origin",
            reason={"basis": "continue"}, evidence={"reviewed": True},
        )
    with sqlite3.connect(db) as connection:
        assert connection.execute(
            "SELECT count(*) FROM memory_selection_recoveries"
        ).fetchone()[0] == 0


@pytest.mark.parametrize("origin_status", [None, "completed"])
def test_open_or_completed_selection_cannot_be_declared_abandoned(db, origin_status):
    accepted_task(db, "task-origin", status=origin_status)
    accepted_task(db, "task-current")
    selected = record_selection(
        db, task_window_id="task-origin", query={"goal": "recover"},
        memory_id="m1", reason={"match": "goal"},
    )
    with pytest.raises(RecoveryUnavailable, match="current exact owner"):
        selection_recovery(
            db, action="declare_abandoned", **recovery_args(db),
            selection_id=selected["selection_id"], prior_task_window_id="task-origin",
            reason={"basis": "guess"}, evidence={"age": "old"},
        )


def test_recovery_cross_owner_denial_uses_same_boundary_and_writes_nothing(db):
    accepted_task(db, "task-origin", status="blocked")
    accepted_task(db, "task-current")
    selected = record_selection(
        db, task_window_id="task-origin", query={"goal": "recover"},
        memory_id="m1", reason={"match": "goal"},
    )
    with pytest.raises(RecoveryUnavailable, match="current exact owner"):
        selection_recovery(
            db, action="declare_abandoned",
            **recovery_args(db, project_id="project-b"),
            selection_id=selected["selection_id"], prior_task_window_id="task-origin",
            reason={"basis": "foreign"}, evidence={"claim": "blocked"},
        )
    with sqlite3.connect(db) as connection:
        assert connection.execute(
            "SELECT count(*) FROM memory_selection_abandonments"
        ).fetchone()[0] == 0


def test_applied_disposition_and_harmful_evaluation_are_atomic(tmp_path):
    from odibi_anchor.codebase.memory_context import append_memory

    path = tmp_path / "atomic.db"
    memory = append_memory(tmp_path, entry_type="gotcha", content="advice", project="private:a", db_path=str(path))
    selected = record_selection(
        path, task_window_id="task-1", query={"goal": "ship"}, memory_id=memory["id"],
        reason={"match": "goal"},
    )
    with pytest.raises(ValueError, match="action"):
        record_disposition_effects(
            path, selection_id=selected["selection_id"], task_window_id="task-1",
            disposition="applied", reason={"basis": "relevant"}, action="",
        )
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM memory_dispositions").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM memory_applications").fetchone()[0] == 0
    disposed, application = record_disposition_effects(
        path, selection_id=selected["selection_id"], task_window_id="task-1",
        disposition="applied", reason={"basis": "relevant"}, action="edit",
    )
    assert disposed["disposition"] == "applied" and application is not None
    evaluated = evaluate_application_effects(
        path, application_id=application["application_id"], task_window_id="task-1",
        outcome="harmful", evidence={"test": "contradicted"},
    )
    with sqlite3.connect(path) as connection:
        assert evaluated["outcome"] == "harmful"
        assert connection.execute("SELECT status FROM memories WHERE id=?", (memory["id"],)).fetchone()[0] == "quarantined"


def test_supersession_requires_eligible_same_project_replacement(tmp_path):
    from odibi_anchor.codebase.memory_context import append_memory

    path = tmp_path / "trust.db"
    old = append_memory(tmp_path, entry_type="gotcha", content="old", project="private:a", db_path=str(path))
    other = append_memory(tmp_path, entry_type="gotcha", content="other", project="private:b", db_path=str(path))
    selected = record_selection(
        path, task_window_id="task-1", query={"goal": "ship"}, memory_id=old["id"],
        reason={"match": "goal"},
    )
    with pytest.raises(ValueError, match="same project/trust domain"):
        record_disposition_effects(
            path, selection_id=selected["selection_id"], task_window_id="task-1",
            disposition="superseded", reason={"basis": "newer"}, replacement_memory_id=other["id"],
        )
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM memory_dispositions").fetchone()[0] == 0
        assert connection.execute("SELECT status FROM memories WHERE id=?", (old["id"],)).fetchone()[0] == "candidate"


def test_lifecycle_payloads_are_redacted_before_persistence(db):
    selected = record_selection(
        db, task_window_id="task-1", query={"token": "secret-value"}, memory_id="m1",
        reason={"match": "goal"},
    )
    applied = record_application(
        db, selection_id=selected["selection_id"], task_window_id="task-1", action="edit",
        context={"path": "/home/user/private"},
    )
    evaluated = evaluate_application(
        db, application_id=applied["application_id"], task_window_id="task-1",
        outcome="helpful", evidence={"authorization": "Bearer secret-value"},
    )
    assert selected["query"] == {"token": "[REDACTED]"}
    assert applied["context"] == {"path": "[REDACTED_PATH]"}
    assert evaluated["evidence"] == {"authorization": "[REDACTED]"}


def test_task_activity_restores_durable_selection_and_application_ids(db):
    selected = record_selection(
        db, task_window_id="task-1", query={"goal": "ship"}, memory_id="m1",
        reason={"match": "goal"},
    )
    applied = record_application(
        db, selection_id=selected["selection_id"], task_window_id="task-1", action="edit",
    )
    assert task_memory_activity(db, task_window_id="task-1") == {
        "memory_selections": [selected["selection_id"]],
        "memory_dispositions": [],
        "memory_applications": [applied["application_id"]],
        "memory_evaluations": [],
    }
    assert task_memory_activity(db, task_window_id="other") == {
        "memory_selections": [], "memory_dispositions": [],
        "memory_applications": [], "memory_evaluations": [],
    }


def test_projection_is_idempotent_and_rejects_conflicting_memory(db):
    first = record_projection(
        db, learning_item_id="lesson1", memory_id="m1", lineage={"assessment": "human-1"}
    )
    assert first == record_projection(
        db, learning_item_id="lesson1", memory_id="m1", lineage={"assessment": "human-1"}
    )
    with pytest.raises(ValueError, match="different memory"):
        record_projection(
            db, learning_item_id="lesson1", memory_id="m2", lineage={"assessment": "human-1"}
        )
    connection = sqlite3.connect(db)
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_lifecycle_schema_can_precede_structured_learning(tmp_path, monkeypatch):
    path = tmp_path / "coexistent.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE memories (id TEXT PRIMARY KEY)")
    connection.commit()
    connection.close()
    initialize_schema(path)
    monkeypatch.setenv("ANCHOR_MEMORY_DB", str(path))

    from odibi_anchor.codebase.structured_learning_context import (
        activate_learning_obligation,
        active_learning_obligation,
        latest_closed_learning_obligation,
    )

    owner = {"project_id": "project:coexist", "task_window_id": "ltw-coexist"}
    assert active_learning_obligation(**owner) is None
    assert latest_closed_learning_obligation(**owner) is None
    obligation = activate_learning_obligation(
        task_window_id="ltw-coexist", session_ref="session:coexist",
        checkpoint_ref="gate:coexist", project_ref="project:coexist",
    )
    assert obligation["status"] == "active"
    with sqlite3.connect(path) as verified:
        domains = {
            row[0] for row in verified.execute("SELECT domain FROM anchor_schema_versions")
        }
    assert domains == {"memory_lifecycle", "structured_learning"}
