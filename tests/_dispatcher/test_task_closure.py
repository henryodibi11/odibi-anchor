"""A task's open or closed state, and its mode-specific ending, through the public dispatcher."""

from __future__ import annotations

import pytest


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    anchor_home = tmp_path.parent / f"{tmp_path.name}-anchor-home"
    monkeypatch.setenv("ANCHOR_HOME", str(anchor_home))
    monkeypatch.setenv("ANCHOR_MEMORY_DB", str(anchor_home / ".agent_memory.db"))
    from odibi_anchor.bootstrap import init

    def start():
        anchor, _, _ = init(root=tmp_path, output_format="dict")
        return anchor

    anchor = start()
    anchor("orient", output_format="dict")
    return anchor, start


def _read_only_task(anchor):
    anchor("new_session", name="question", inline=True, output_format="dict")
    return anchor(
        "task", "Investigate one bounded read-only question.", goal="Answer the question and finish.",
        mode="analysis", acceptance_criteria=["Answer the question."], output_format="dict",
    )


def _artifact_task(anchor):
    accepted = anchor(
        "task", "Record one managed note.", goal="Write and close one artifact change.",
        mode="documentation", work_type="change", execution_mode="artifact_only",
        risk="low", rigor="direct", continuation=True,
        acceptance_criteria=["The note is recorded."], output_format="dict",
    )
    anchor("skill_loaded", "documentation", output_format="dict")
    return accepted


def test_read_only_task_reports_open_until_assessment_closes_it_as_completed(runtime):
    anchor, _ = runtime
    accepted = _read_only_task(anchor)
    window = accepted["task_window_id"]

    closure = accepted["operating_protocol"]["task_closure"]
    assert closure["state"] == "open" and closure["task_window_id"] == window
    assert closure["complete_with"] == ['anchor("learning", "assess", outcome="...")']
    assert closure["stop_with"] == ['anchor("learning", "safe_stop", status="blocked", reason="...")']
    assert closure["resume_with"] == f"anchor('task_rebind', task_window_id={window!r})"
    # A read-only task has no file checks or gate, so its next step is closure, not preflight.
    assert "learning.assess" in accepted["envelope"]["next_operation"]["copy_ready"]
    status = anchor("status", output_format="dict")
    assert status["operating_protocol"]["task_closure"]["state"] == "open"
    assert not any("Session clean" in item for item in status["suggested_next_actions"])
    assert any(f"Task {window} is still open" in item for item in status["suggested_next_actions"])

    assessed = anchor("learning", "assess", outcome="nothing_reusable_learned", output_format="dict")

    assert assessed["accepted_task_closure"]["status"] == "closed"
    assert assessed["operating_protocol"]["task_closure"] == {
        "task_window_id": window, "execution_mode": "read_only", "workflow_id": None,
        "state": "closed", "closed_as": "completed",
    }
    assert assessed["operating_protocol"]["terminal_return"] == {
        "status": "ready", "basis": "no_change_assessment_committed",
    }
    assert assessed["envelope"]["next_operation"] is None
    with pytest.raises(RuntimeError, match="already closed as completed"):
        anchor("learning", "safe_stop", status="blocked", reason="late", output_format="dict")


def test_read_only_task_stops_as_blocked_without_claiming_completion(runtime):
    anchor, start = runtime
    window = _read_only_task(anchor)["task_window_id"]

    stopped = anchor("learning", "safe_stop", status="blocked", reason="owner unavailable", output_format="dict")

    assert stopped["terminal_task_record"]["record"]["terminal"]["status"] == "blocked"
    assert stopped["metrics"]["learning_assessed"] is False
    assert stopped["operating_protocol"]["task_closure"]["closed_as"] == "blocked"
    restarted = start()
    assert restarted("orient", output_format="dict").get("open_tasks") in (None, [])
    with pytest.raises(RuntimeError, match="no open accepted task"):
        restarted("task_rebind", task_window_id=window, output_format="dict")


def test_task_left_open_is_discovered_and_resumable_after_restart(runtime):
    anchor, start = runtime
    window = _read_only_task(anchor)["task_window_id"]

    restarted = start()
    before_rebind = restarted("status", output_format="dict")
    assert not any("ready for delivery" in item for item in before_rebind["suggested_next_actions"])
    assert any("task_rebind" in item for item in before_rebind["suggested_next_actions"])
    with pytest.raises(RuntimeError, match="task_rebind"):
        restarted("learning", "assess", outcome="nothing_reusable_learned", output_format="dict")
    oriented = restarted("orient", output_format="dict")
    assert [task["task_window_id"] for task in oriented["open_tasks"]] == [window]
    assert oriented["next_operation"]["kwargs"] == {"task_window_id": window}
    restarted("task_rebind", task_window_id=window, output_format="dict")
    status = restarted("status", output_format="dict")

    assert status["operating_protocol"]["task_closure"]["state"] == "open"
    assert status["operating_protocol"]["task_closure"]["task_window_id"] == window


def test_write_task_lists_gate_before_closure_and_stops_after_assessment(runtime):
    anchor, _ = runtime
    _artifact_task(anchor)

    closure = anchor("status", output_format="dict")["operating_protocol"]["task_closure"]
    assert closure["remaining"] == ["task_window_gate", "learning_closure"]
    assert closure["complete_with"] == ['anchor("gate")', 'anchor("learning", "assess", outcome="...")']
    assert closure["stop_with"] == [
        'anchor("learning", "assess", outcome="...")',
        'anchor("learning", "safe_stop", status="blocked", reason="...")',
    ]
    assessed = anchor("learning", "assess", outcome="nothing_reusable_learned", output_format="dict")
    # Assessment without a passing gate does not close write-capable work.
    assert assessed.get("accepted_task_closure") is None
    assert assessed["operating_protocol"]["task_closure"]["stop_with"] == [
        'anchor("learning", "safe_stop", status="blocked", reason="...")',
    ]
    stopped = anchor("learning", "safe_stop", status="failed", reason="cancelled by owner", output_format="dict")
    assert stopped["operating_protocol"]["task_closure"]["closed_as"] == "failed"


def test_work_item_amend_records_the_accepted_task_and_needs_write_authority(runtime):
    anchor, _ = runtime
    accepted = _artifact_task(anchor)
    anchor("problem", "create", title="Exports are slow", output_format="dict")
    item = anchor("work_item", "create", title="Fix exports", outcome="Faster exports",
                  problem_record="PRB-2026-0001", output_format="dict")
    item_id = item["work_item_id"]
    closed = anchor("work_item", "close", item_id, outcome="completed", implementation_disposition="implemented",
                    output_format="dict")

    amended = anchor("work_item", "amend", item_id, pr_url="https://github.com/org/repo/pull/7",
                     reason={"why": "PR link missed at close"}, _task_window_id="ltw_forged",
                     write_version=2, expected_revision=closed["revision"],
                     output_format="dict")

    assert amended["envelope"]["outcome"] == "succeeded"
    assert amended["amendment"]["task_window_id"] == accepted["task_window_id"]
    assert amended["status"] == "completed"
    _read_only_task(anchor)
    with pytest.raises(RuntimeError, match="artifact_write is incompatible"):
        anchor("work_item", "amend", item_id, pr_url="", reason={"why": "x"}, output_format="dict")


def test_read_only_task_with_recorded_changes_offers_only_a_stop(runtime, tmp_path):
    anchor, _ = runtime
    _read_only_task(anchor)
    (tmp_path / "a.py").write_text("VALUE = 1\n", encoding="utf-8")
    from odibi_anchor._utils._session_state import _SESSION_FILES_CHANGED

    _SESSION_FILES_CHANGED.add("a.py")  # what the write guard records for this write
    try:
        closure = anchor("status", output_format="dict")["operating_protocol"]["task_closure"]
        assert closure["complete_with"] is None
        assert "read_only_changes_recorded" in closure["remaining"]
        assessed = anchor("learning", "assess", outcome="nothing_reusable_learned", output_format="dict")
        # Assessment does not close a read-only task with changes, and the projection says so.
        assert assessed.get("accepted_task_closure") is None
        assert assessed["operating_protocol"]["task_closure"]["stop_with"] == [
            'anchor("learning", "safe_stop", status="blocked", reason="...")',
        ]
        assert "safe_stop" in assessed["envelope"]["next_operation"]["copy_ready"]
        stopped = anchor("learning", "safe_stop", status="failed", reason="wrote under read-only",
                         output_format="dict")
        assert stopped["operating_protocol"]["task_closure"]["closed_as"] == "failed"
    finally:
        _SESSION_FILES_CHANGED.discard("a.py")
