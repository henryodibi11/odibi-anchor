"""Help and accepted tasks expose mode contracts; gate and assess report pending owner decisions."""

import sqlite3
from types import SimpleNamespace

import pytest


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    from odibi_anchor._dispatcher._project import project_action
    from odibi_anchor.bootstrap import init

    home, target = tmp_path / "home", tmp_path / "target"
    home.mkdir()
    target.mkdir()
    monkeypatch.setenv("ANCHOR_HOME", str(home))
    monkeypatch.setenv("ANCHOR_MEMORY_DB", str(home / "memory.db"))
    monkeypatch.setenv("ANCHOR_TRUST_DOMAIN", "personal")
    project_action(home, "create", name="alpha", target=target, output_format="dict")
    anchor, _, _ = init(root=str(target), project="alpha", output_format="dict")
    anchor("orient", output_format="dict")
    return SimpleNamespace(anchor=anchor, home=home, target=target)


def _accept(anchor, name):
    anchor("new_session", name=name, inline=True, output_format="dict")
    return anchor("task", "Read the module layout", goal="Inspect module layout", mode="analysis",
                  risk="low", rigor="direct", acceptance_criteria=["Layout described"],
                  output_format="dict")


def _gate(anchor):
    anchor("review", output_format="dict")
    return anchor("gate", output_format="dict")


def test_help_mode_returns_a_contract_and_the_mode_table(runtime):
    from odibi_anchor.planning._task_profile import _LEGACY_DEFAULTS

    contract = runtime.anchor("help", "mode", "pipeline", output_format="dict")
    assert contract["kind"] == "mode_contract" and contract["mode"] == "pipeline"
    assert contract["execution_mode"] == "source_change" and contract["workflow_required"] is True
    assert "artifact_sha256" not in contract["accepted_criteria"]
    table = runtime.anchor("help", "mode", output_format="dict")
    assert {row["mode"] for row in table["modes"]} == set(_LEGACY_DEFAULTS)
    task_help = str(runtime.anchor("help", "task", output_format="dict"))
    assert 'anchor("help", "mode", "<mode>")' in task_help and "mode_contract" in task_help


def test_accepted_task_carries_its_mode_contract(runtime):
    accepted = _accept(runtime.anchor, "contract")
    contract = accepted["mode_contract"]
    assert (contract["mode"], contract["execution_mode"], contract["risk"], contract["rigor"]) == (
        "analysis", "read_only", "low", "direct")
    assert contract["workflow_required"] is False
    assert contract["review_requirement"]["plan_reviews_before_owner_approval"] == 1
    assert "mode_contract" not in str(accepted["accepted_task_authority"])


def test_gate_and_assess_report_pending_owner_decisions(runtime):
    from odibi_anchor.codebase.memory_context import append_memory

    anchor = runtime.anchor
    _accept(anchor, "lessons")
    empty = _gate(anchor)["pending_owner_decisions"]
    assert empty["status"] == "ready" and empty["observations"]["total"] == 0
    captured = anchor(
        "learning", "capture", observation_type="friction",
        summary="The fixture needed an owner decision", signal_key="fixture.owner-decision",
        applicability_scope="project_local", project_refs=["alpha"],
        evidence=[{"reference_type": "session", "reference": "fixture-session"}], output_format="dict",
    )
    observation_id = captured["observation_id"]
    memory = append_memory(runtime.target, entry_type="gotcha", content="Fixture candidate memory",
                           project="alpha", db_path=str(runtime.home / "memory.db"))
    memory_id = memory.get("id") or memory.get("memory_id")
    assessed = anchor("learning", "assess", outcome="observations_recorded",
                      observation_ids=[observation_id], output_format="dict")
    assert "pending_owner_decisions" not in str(assessed["terminal_task_record"])

    _accept(anchor, "next")
    gated = _gate(anchor)
    for pending in (assessed["pending_owner_decisions"], gated["pending_owner_decisions"]):
        assert pending["status"] == "ready" and pending["project"] == "alpha"
        observation = next(i for i in pending["observations"]["items"] if i["id"] == observation_id)
        assert pending["observations"]["total"] >= 1
        assert observation["summary"] == "The fixture needed an owner decision"
        assert observation["recurrence_count"] == 1 and observation["attention_due"] is False
        assert [o["decision"] for o in observation["options"]] == ["derive_lesson", "defer", "reject"]
        for option in observation["options"]:
            assert option["action"] == "learning" and option["args"] == ["triage"]
            assert option["kwargs"]["actor_kind"] == "human" and option["requires_owner"] is True
            assert "anchor('learning', 'triage'" in option["copy_ready"]
        candidate = next(i for i in pending["candidate_memories"]["items"] if i["id"] == memory_id)
        assert candidate["summary"] == "Fixture candidate memory"
        options = {o["decision"]: o for o in candidate["options"]}
        assert options["activate"]["kwargs"] == {"command": "request_owner_activation", "memory_id": memory_id}
        assert options["reject"]["copy_ready"] == f"anchor('reject', {memory_id!r})"

    # The listed operation works once the owner's answer replaces the placeholders.
    # Triage writes a learning record, so it runs in a task that may write artifacts.
    anchor("learning", "assess", outcome="nothing_reusable_learned", output_format="dict")
    anchor("new_session", name="record-decision", inline=True, output_format="dict")
    anchor("task", "Record the owner's lesson decision", goal="Record owner decision on lessons",
           mode="retrospective", risk="low", rigor="direct", acceptance_criteria=["Decision recorded"],
           output_format="dict")
    defer = next(o for o in observation["options"] if o["decision"] == "defer")
    owner = {"actor_ref": "owner", "decision_source": "fixture-review", "rationale": "Later"}
    anchor(defer["action"], *defer["args"], **{**defer["kwargs"], **owner}, output_format="dict")
    after = _gate(anchor)["pending_owner_decisions"]
    assert observation_id not in [i["id"] for i in after["observations"]["items"]]


def test_broken_learning_store_degrades_without_failing_the_gate(runtime, monkeypatch):
    from odibi_anchor._dispatcher import _owner_decisions

    def broken(*_args):
        raise sqlite3.DatabaseError("file is not a database")

    monkeypatch.setattr(_owner_decisions, "_read_pending", broken)
    _accept(runtime.anchor, "degraded")
    gated = _gate(runtime.anchor)
    assert gated["pending_owner_decisions"]["status"] == "degraded"
    assert "DatabaseError" in gated["pending_owner_decisions"]["reason"]


def test_corrupt_database_file_yields_degraded_marker(tmp_path):
    from odibi_anchor._dispatcher._owner_decisions import pending_owner_decisions

    corrupt = tmp_path / "memory.db"
    corrupt.write_bytes(b"not a sqlite database" * 64)
    result = pending_owner_decisions(corrupt, project="alpha")
    assert result["kind"] == "pending_owner_decisions" and result["status"] == "degraded"
