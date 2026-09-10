"""Operator contracts for distinct-project concurrency rollout and recovery."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from odibi_anchor._dispatcher._concurrency import DOMAINS, SELECTION_STATES, concurrency_action
from odibi_anchor._dispatcher._project import (
    project_action,
    resolve_active_project,
    resolve_route_binding,
)
from odibi_anchor.codebase._memory_lifecycle import (
    _V3_DDL,
    DOMAIN,
    V2_SCHEMA_SHA256,
    initialize_schema,
    rollback_recovery_schema,
    selection_recovery_migration_status,
)


def _runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "anchor-home"
    home.mkdir()
    target = tmp_path / "target-alpha"
    target.mkdir()
    project_action(
        home,
        "create",
        name="project-alpha",
        target=target,
        output_format="dict",
    )
    resolved = resolve_active_project(home, "project-alpha")
    binding = resolve_route_binding(
        home,
        project="project-alpha",
        target_hint=target,
        runtime_instance_id="runtime-alpha",
    )
    assert resolved is not None and binding is not None
    memory_db = home / ".agent_memory.db"
    monkeypatch.setenv("ANCHOR_MEMORY_DB", str(memory_db))
    state = SimpleNamespace(
        active_project="project-alpha",
        learning_owner_project_id="project-alpha",
        anchor_home=str(home),
        project_root=resolved["project_root"],
        artifact_root=resolved["artifact_root"],
        target_root=resolved["target_root"],
        runtime_instance_id="runtime-alpha",
        session_id="session-alpha",
        task_window_id="task-alpha",
        continuity_generation=0,
        continuity_record_sha256=None,
        continuity_status="uninitialized",
        repository_provider=None,
        trust_domain=None,
    )
    return binding, state, memory_db


def _snapshot(root: Path) -> dict[str, bytes | None]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes() if path.is_file() else None
        for path in sorted(root.rglob("*"))
    }


@pytest.mark.parametrize("command", ["inspect", "dry_run"])
def test_inspection_is_strictly_read_only_on_uninitialized_domains(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    binding, state, memory_db = _runtime(tmp_path, monkeypatch)
    before = _snapshot(tmp_path)

    result = concurrency_action(
        command=command,
        route_binding=binding,
        session_state=state,
        memory_db=memory_db,
    )

    assert _snapshot(tmp_path) == before
    assert not memory_db.exists()
    assert result["atomic"] is False
    assert [item["domain"] for item in result["domains"]] == list(DOMAINS)
    assert result["domains"][0]["status"] == "exact"
    assert result["domains"][1]["schema_status"] == "uninitialized"
    assert result["domains"][2]["schema_status"] == "uninitialized"
    selection = result["domains"][3]
    assert selection["schema_status"] == "uninitialized"
    assert selection["state_labels"] == list(SELECTION_STATES)
    assert selection["metric_interpretation"] == "neutral lifecycle total, not a leak count"


def test_apply_is_idempotent_and_never_claims_release_qualification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding, state, memory_db = _runtime(tmp_path, monkeypatch)
    with sqlite3.connect(memory_db) as connection:
        connection.execute("CREATE TABLE memories (id TEXT PRIMARY KEY, project TEXT)")

    first = concurrency_action(
        command="apply",
        route_binding=binding,
        session_state=state,
        memory_db=memory_db,
    )
    second = concurrency_action(
        command="apply",
        route_binding=binding,
        session_state=state,
        memory_db=memory_db,
    )

    assert first["support_status"] == second["support_status"] == "release_evidence_required"
    assert first["atomic"] is second["atomic"] is False
    assert [item["apply_status"] for item in second["domains"]] == [
        "verified_no_migration",
        "verified",
        "verified",
        "verified",
    ]
    assert second["domains"][2]["schema_status"] == "exact"
    assert second["domains"][3]["schema_status"] == "exact"


def test_stale_route_is_visible_but_apply_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding, state, memory_db = _runtime(tmp_path, monkeypatch)
    replacement = tmp_path / "replacement"
    replacement.mkdir()
    project_action(
        binding.anchor_home,
        "set_target",
        name=binding.project_id,
        target=replacement,
        output_format="dict",
    )

    inspected = concurrency_action(
        "inspect",
        domains=["route"],
        route_binding=binding,
        session_state=state,
        memory_db=memory_db,
    )
    applied = concurrency_action(
        "apply",
        domains=["route", "continuity"],
        route_binding=binding,
        session_state=state,
        memory_db=memory_db,
    )

    assert inspected["domains"][0]["status"] == "stale"
    assert inspected["support_status"] == "blocked"
    assert applied["support_status"] == "blocked"
    assert [item["domain"] for item in applied["domains"]] == ["route"]
    assert applied["domains"][0]["status"] == "blocked"


def test_rollback_is_one_domain_only_and_preserves_additive_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding, state, memory_db = _runtime(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="exactly one"):
        concurrency_action(
            command="rollback",
            route_binding=binding,
            session_state=state,
            memory_db=memory_db,
        )

    result = concurrency_action(
        command="rollback",
        domains=["continuity"],
        route_binding=binding,
        session_state=state,
        memory_db=memory_db,
    )

    assert result["atomic"] is False
    assert result["domains"][0]["status"] == "unavailable"
    packet = result["domains"][0]["manual_recovery"]
    assert packet["status"] == "manual_recovery_required"
    assert "do not delete or rewrite history" in packet["next_actions"][-1]


def test_legacy_selection_rollback_does_not_migrate_as_a_side_effect(tmp_path: Path) -> None:
    memory_db = tmp_path / "memory.db"
    with sqlite3.connect(memory_db) as connection:
        connection.executescript(
            "CREATE TABLE memories (id TEXT PRIMARY KEY, project TEXT);"
            "CREATE TABLE learning_items (item_id TEXT PRIMARY KEY);"
        )
    initialize_schema(memory_db)
    with sqlite3.connect(memory_db) as connection:
        for statement in reversed(_V3_DDL):
            kind, name = statement.split()[1:3]
            connection.execute(f"DROP {kind} IF EXISTS {name.split('(', 1)[0]}")
        connection.execute(
            "UPDATE anchor_schema_versions SET version=2,schema_sha256=? WHERE domain=?",
            (V2_SCHEMA_SHA256, DOMAIN),
        )
    before = memory_db.read_bytes()

    status = selection_recovery_migration_status(memory_db)
    with pytest.raises(RuntimeError, match="no eligible migration"):
        rollback_recovery_schema(memory_db)

    assert status["schema_status"] == "legacy_v2"
    assert status["planned_action"] == "migrate_v2_to_v3"
    assert memory_db.read_bytes() == before


def test_markdown_states_non_atomic_distinct_project_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding, state, memory_db = _runtime(tmp_path, monkeypatch)

    rendered = concurrency_action(
        "inspect",
        domains=["route"],
        route_binding=binding,
        session_state=state,
        memory_db=memory_db,
        output_format="markdown",
    )

    assert "Cross-domain atomicity: `false`" in rendered
    assert "only to distinct managed projects" in rendered
