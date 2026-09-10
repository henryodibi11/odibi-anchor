"""Regression coverage for use-phase lifecycle hardening."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from odibi_anchor._dispatcher._limits import lifecycle_limits
from odibi_anchor._dispatcher._safe_stop import safe_stop_task


@pytest.mark.parametrize(
    ("risk", "expected"),
    [("low", 20), ("medium", 15), ("high", 10), ("critical", 8)],
)
def test_checkpoint_limit_is_risk_aware(tmp_path, risk, expected):
    profile = SimpleNamespace(risk=risk)
    assert lifecycle_limits(str(tmp_path), profile)["checkpoint_file_threshold"] == expected


def test_project_limit_is_validated_and_remains_upper_bound(tmp_path):
    (tmp_path / ".anchor_config.json").write_text(
        '{"enforcement":{"checkpoint_file_threshold":30,"max_ungated_edits":40}}',
        encoding="utf-8",
    )
    limits = lifecycle_limits(str(tmp_path), SimpleNamespace(risk="high"))
    assert limits == {
        "checkpoint_file_threshold": 15,
        "checkpoint_configured_upper_bound": 30,
        "checkpoint_risk": "high",
        "max_ungated_edits": 40,
    }
    (tmp_path / ".anchor_config.json").write_text(
        '{"enforcement":{"checkpoint_file_threshold":true}}', encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="must be an integer"):
        lifecycle_limits(str(tmp_path), SimpleNamespace(risk="low"))


def test_safe_stop_is_terminal_without_passing_gate(monkeypatch):
    import odibi_anchor.codebase.structured_learning_context as learning

    monkeypatch.setattr(learning, "active_learning_obligation", lambda **_owner: None)
    monkeypatch.setattr(
        learning, "latest_closed_learning_obligation",
        lambda **owner: {
            "task_window_id": owner["task_window_id"], "status": "assessed",
        },
    )
    state = SimpleNamespace(
        active_task_profile=object(), active_project="project-safe-stop",
        task_window_id="ltw_safe_stop",
        task_verification_epoch=0, task_goal="Qualify unavailable host evidence",
        terminal_status=None, terminal_basis=None, terminal_reason=None,
    )
    result = safe_stop_task(
        status="blocked", reason="Host API cannot provide repository evidence.",
        unavailable_evidence=["Local Git state unavailable"],
        learning_project_id="project-safe-stop",
        session_state=state, session_timings=[{"action": "review", "passed": True}],
    )
    assert result["metrics"] == {
        "terminal_status": "blocked", "gate_passed": False,
        "learning_assessed": True, "unavailable_evidence_count": 1,
    }
    assert state.terminal_status == "blocked" and state.terminal_basis == "safe_stop"


def test_safe_stop_rejects_active_learning_and_successful_gate(monkeypatch):
    import odibi_anchor.codebase.structured_learning_context as learning

    state = SimpleNamespace(
        active_task_profile=object(), active_project="project-safe-stop",
        task_window_id="ltw_safe_stop",
        task_verification_epoch=0, task_goal="Blocked task",
        terminal_status=None, terminal_basis=None, terminal_reason=None,
    )
    monkeypatch.setattr(
        learning, "active_learning_obligation", lambda **_owner: {"status": "active"},
    )
    with pytest.raises(RuntimeError, match="assessment first"):
        safe_stop_task(
            status="blocked", reason="Blocked", unavailable_evidence=None,
            learning_project_id="project-safe-stop",
            session_state=state, session_timings=[],
        )
    monkeypatch.setattr(learning, "active_learning_obligation", lambda **_owner: None)
    monkeypatch.setattr(
        learning, "latest_closed_learning_obligation",
        lambda **_owner: {"status": "assessed"},
    )
    with pytest.raises(RuntimeError, match="successful delivery gate"):
        safe_stop_task(
            status="failed", reason="Failed", unavailable_evidence=None,
            learning_project_id="project-safe-stop",
            session_state=state, session_timings=[{"action": "gate", "passed": True}],
        )


def test_explicit_continuation_creates_fresh_task_without_new_session(tmp_path, monkeypatch):
    monkeypatch.setenv("ANCHOR_HOME", str(tmp_path / "anchor-home"))
    monkeypatch.setenv("ANCHOR_MEMORY_DB", str(tmp_path / "anchor-home" / ".agent_memory.db"))
    from odibi_anchor.bootstrap import init

    anchor, _, _ = init(root=tmp_path, output_format="dict")
    anchor("orient", output_format="dict")
    task = anchor(
        "task", "Continue the bounded documentation task with fresh authority.",
        goal="Verify continuation omits only the redundant inline session call.",
        mode="documentation", risk="low", rigor="direct", continuation=True,
        acceptance_criteria=["A fresh task window and synthetic session marker exist."],
        output_format="dict",
    )
    for required in task["required_skills"]:
        anchor("skill_loaded", required["skill"], output_format="dict")
    from odibi_anchor._utils._session_state import _SESSION_STATE, _SESSION_TIMINGS

    assert _SESSION_STATE.task_window_id.startswith("ltw_")
    assert any(
        row.get("synthetic") == "explicit_continuation" for row in _SESSION_TIMINGS
    )
    assert _SESSION_STATE.learning_obligation_id is None
    anchor(
        "learning", "assess", outcome="nothing_reusable_learned", output_format="dict",
    )


def test_learning_assessment_requires_evaluation_for_applied_task_memory(
    tmp_path, monkeypatch,
):
    memory_db = tmp_path / "anchor-home" / ".agent_memory.db"
    monkeypatch.setenv("ANCHOR_HOME", str(tmp_path / "anchor-home"))
    monkeypatch.setenv("ANCHOR_MEMORY_DB", str(memory_db))
    from odibi_anchor.bootstrap import init
    from odibi_anchor.codebase.memory_context import append_memory

    append_memory(
        tmp_path,
        entry_type="gotcha",
        content="Compatibility changes require an installed-package probe.",
        tags=["compatibility"],
        project="all",
        db_path=str(memory_db),
    )
    anchor, _, _ = init(root=tmp_path, output_format="dict")
    anchor("orient", output_format="dict")
    task = anchor(
        "task",
        "Change compatibility behavior and qualify the installed package.",
        goal="Preserve compatibility using an installed-package probe.",
        mode="documentation",
        risk="low",
        rigor="direct",
        continuation=True,
        acceptance_criteria=["The installed-package probe passes."],
        output_format="dict",
    )
    for required in task["required_skills"]:
        anchor("skill_loaded", required["skill"], output_format="dict")
    selected = task["memory_context"]["selections"]
    assert len(selected) == 1
    disposed = anchor(
        "memory",
        "disposition",
        selection_id=selected[0]["selection_id"],
        disposition="applied",
        reason={"basis": "The compatibility warning shaped verification."},
        action="add installed-package probe",
        output_format="dict",
    )
    application = disposed["results"][0]["application"]
    anchor("gate", output_format="dict")

    with pytest.raises(RuntimeError, match="require evidence-backed evaluation"):
        anchor(
            "learning",
            "assess",
            outcome="nothing_reusable_learned",
            output_format="dict",
        )

    anchor(
        "memory",
        "evaluate",
        application_id=application["application_id"],
        outcome="helpful",
        evidence={"test": "installed-package probe passed"},
        output_format="dict",
    )
    assessment = anchor(
        "learning",
        "assess",
        outcome="nothing_reusable_learned",
        output_format="dict",
    )
    assert assessment["assessment"]["outcome"] == "nothing_reusable_learned"
