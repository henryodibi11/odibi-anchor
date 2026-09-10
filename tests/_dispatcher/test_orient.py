"""Tests for anchor('orient') — AXI pre-computed orientation aggregate."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from odibi_anchor.bootstrap import init
from odibi_anchor.planning import render_task_execution_report


def test_orient_aggregates_status_audit_and_defers_memory(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    anchor, _root, _m = init(root=str(tmp_path))
    o = anchor("orient", output_format="dict")
    assert o["kind"] == "orientation"
    for key in ("status", "memory", "audit_history"):
        assert key in o
    assert o["memory"] == {
        "status": "deferred",
        "reason": "bounded task-aware retrieval occurs at task acceptance",
    }
    assert o["artifact_contract"]["scope"] == "all_managed_projects"
    assert o["artifact_contract"]["existing_record_rewrites_required"] is False
    assert {item["path"] for item in o["artifact_contract"]["artifacts"]} == {
        "PROJECT.md", "source/", "notebooks/", "problems/", "specs/",
        "work_items/", "decisions/", "archive/",
    }
    assert o["capture_standards"]["version"] == "1.1"
    assert "terminal_return" in {
        route["record"] for route in o["capture_standards"]["routes"]
    }


def test_orient_markdown(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    anchor, _root, _m = init(root=str(tmp_path))
    om = anchor("orient", output_format="markdown")
    assert isinstance(om, str)
    assert om.startswith("# Orientation")
    assert "Managed artifact contract v" in om
    assert "`decisions/`" in om
    assert "Capture standards v1.1" in om
    assert "**Common fields:**" in om
    assert "Do not use for:" in om
    assert "### Offline references" in om


def test_orient_records_prerequisite_timings(tmp_path, monkeypatch):
    # orient must route status/audit_history through anchor() so the
    # task-prerequisite gate sees their timings.
    monkeypatch.setenv("HOME", str(tmp_path))
    anchor, _root, _m = init(root=str(tmp_path))
    anchor("orient", output_format="dict")
    from odibi_anchor._utils._session_state import _SESSION_TIMINGS
    actions = {t["action"] for t in _SESSION_TIMINGS}
    assert {"status", "audit_history"}.issubset(actions)
    assert "memory" not in actions


def test_new_session_retains_the_orientation_needed_by_task(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    anchor, _root, _m = init(root=str(tmp_path))
    anchor("orient", output_format="dict")
    anchor("new_session", name="oriented-session", inline=True, output_format="dict")

    from odibi_anchor._utils._session_state import _SESSION_TIMINGS

    assert [timing["action"] for timing in _SESSION_TIMINGS] == [
        "status", "audit_history", "new_session",
    ]
    task = anchor(
        "task", "Inspect the oriented session.",
        goal="Prove the documented startup sequence grants task authority.",
        mode="analysis", acceptance_criteria=["The task is accepted."],
        output_format="dict",
    )
    assert task["kind"] == "task_execution_context"


def test_task_exposes_same_runtime_artifact_contract(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    anchor, _root, _m = init(root=str(tmp_path))
    orientation = anchor("orient", output_format="dict")
    anchor("new_session", name="artifact_contract", inline=True)

    task = anchor(
        "task",
        "Document a bounded project decision.",
        goal="Retain a consequential choice across sessions.",
        mode="planning",
        in_scope=["Record the accepted choice."],
        out_of_scope=["Change product source."],
        constraints=["Preserve existing records."],
        risks=["The decision could be stored in the wrong artifact."],
        deliverables=["One durable decision record."],
        acceptance_criteria=["The record has rationale and reversal conditions."],
        output_format="dict",
    )

    assert task["artifact_contract"] == orientation["artifact_contract"]
    assert task["capture_guidance"]["version"] == "1.1"
    assert "decision" in {
        route["record"] for route in task["capture_guidance"]["routes"]
    }
    rendered = render_task_execution_report(task)
    assert "Managed Artifact Contract v" in rendered
    assert "`decisions/`" in rendered
    assert "Capture Guidance v1.1" in rendered


def test_neutral_lifecycle_protocol_uses_committed_state_once_without_rewrites(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    managed = tmp_path / "PROJECT.md"
    managed.write_text("existing managed record\n", encoding="utf-8")
    anchor, _root, _m = init(root=str(tmp_path))

    orientation = anchor("orient", output_format="dict")
    assert orientation["operating_protocol"]["authority"]["task_active"] is False
    anchor("new_session", name="protocol-lifecycle", inline=True)
    task_kwargs = dict(
        goal="Produce a bounded inspection report.", mode="analysis",
        in_scope=["Inspect current state."], out_of_scope=["Modify source or data."],
        constraints=["Preserve managed records."], risks=["Incomplete evidence."],
        deliverables=["Inspection report."], acceptance_criteria=["Evidence is cited."],
    )
    task = anchor("task", "Inspect the current project.", output_format="dict", **task_kwargs)
    assert task["operating_protocol"]["authority"]["task_active"] is True
    assert task["operating_protocol"]["authority"]["task_window_id"]
    markdown = anchor("task", "Inspect the current project.", output_format="markdown", **task_kwargs)
    assert markdown.count("## Operating Protocol v1") == 1
    assert managed.read_text(encoding="utf-8") == "existing managed record\n"
