"""Tests for odibi_anchor.planning.handoff_context."""

import pytest

from odibi_anchor.planning.handoff_context import (
    handoff_context,
    render_handoff_report,
)


# Standard contract keys
_CONTRACT_KEYS = {"kind", "subject", "summary", "metrics", "findings", "risks", "samples", "suggested_next_actions"}


class TestHandoffContextContract:
    """Verify output contract on valid calls."""

    def test_minimal_call__has_all_contract_keys(self):
        ctx = handoff_context("Build silver pipeline")
        assert _CONTRACT_KEYS.issubset(ctx.keys())
        assert ctx["kind"] == "handoff_context"

    def test_subject_defaults_to_task_truncated(self):
        task = "x" * 100
        ctx = handoff_context(task)
        assert ctx["subject"] == task[:50].strip()

    def test_explicit_subject_used(self):
        ctx = handoff_context("Build pipeline", subject="silver_build")
        assert ctx["subject"] == "silver_build"

    def test_state_stored(self):
        ctx = handoff_context("task", state="blocked")
        assert ctx["state"] == "blocked"

    def test_goal_stored(self):
        ctx = handoff_context("task", goal="Deliver merged output")
        assert ctx["goal"] == "Deliver merged output"

    def test_samples_is_empty_dict(self):
        ctx = handoff_context("task")
        assert ctx["samples"] == {}


class TestHandoffContextValidation:
    """Parameter validation tests."""

    def test_invalid_state__raises_value_error(self):
        with pytest.raises(ValueError, match="state must be one of"):
            handoff_context("task", state="invalid_state")

    def test_empty_task__raises_value_error(self):
        with pytest.raises(ValueError, match="task must be a non-empty string"):
            handoff_context("")

    def test_whitespace_task__raises_value_error(self):
        with pytest.raises(ValueError, match="task must be a non-empty string"):
            handoff_context("   ")

    def test_invalid_output_format__raises_value_error(self):
        with pytest.raises(ValueError):
            handoff_context("task", output_format="xml")

    def test_evidence_chain_missing_tool__raises(self):
        with pytest.raises(ValueError, match="evidence_chain\\[0\\] must have"):
            handoff_context("task", evidence_chain=[{"summary": "done"}])

    def test_evidence_chain_missing_summary__raises(self):
        with pytest.raises(ValueError, match="evidence_chain\\[0\\] must have"):
            handoff_context("task", evidence_chain=[{"tool": "profile"}])

    def test_evidence_chain_not_dict__raises(self):
        with pytest.raises(ValueError, match="evidence_chain\\[0\\] must be a dict"):
            handoff_context("task", evidence_chain=["not_a_dict"])

    def test_artifacts_missing_path__raises(self):
        with pytest.raises(ValueError, match="artifacts\\[0\\] must have"):
            handoff_context("task", artifacts=[{"role": "source"}])

    def test_artifacts_missing_role__raises(self):
        with pytest.raises(ValueError, match="artifacts\\[0\\] must have"):
            handoff_context("task", artifacts=[{"path": "/a/b.py"}])

    def test_artifacts_not_dict__raises(self):
        with pytest.raises(ValueError, match="artifacts\\[0\\] must be a dict"):
            handoff_context("task", artifacts=["invalid"])


class TestHandoffContextStates:
    """All valid states are accepted."""

    @pytest.mark.parametrize("state", ["not_started", "in_progress", "blocked", "ready_for_review", "complete"])
    def test_valid_state__accepted(self, state):
        ctx = handoff_context("task", state=state)
        assert ctx["state"] == state


class TestHandoffContextMetrics:
    """Verify metrics computation."""

    def test_metrics__empty_inputs(self):
        ctx = handoff_context("task")
        m = ctx["metrics"]
        assert m["decision_count"] == 0
        assert m["blocker_count"] == 0
        assert m["evidence_step_count"] == 0
        assert m["artifact_count"] == 0
        assert m["open_question_count"] == 0
        assert m["skip_count"] == 0
        assert m["is_blocked"] is False
        assert m["is_actionable"] is False  # no next_action

    def test_metrics__with_data(self):
        ctx = handoff_context(
            "task",
            decisions=["d1", "d2"],
            blockers=["b1"],
            evidence_chain=[{"tool": "t1", "summary": "s1"}],
            artifacts=[{"path": "p1", "role": "source"}],
            open_questions=["q1", "q2"],
            skip=["s1"],
            next_action="next step",
        )
        m = ctx["metrics"]
        assert m["decision_count"] == 2
        assert m["blocker_count"] == 1
        assert m["evidence_step_count"] == 1
        assert m["artifact_count"] == 1
        assert m["open_question_count"] == 2
        assert m["skip_count"] == 1
        assert m["is_blocked"] is True
        # has blocker so not actionable even with next_action
        assert m["is_actionable"] is False

    def test_metrics__actionable_when_no_blockers(self):
        ctx = handoff_context("task", next_action="do something")
        assert ctx["metrics"]["is_actionable"] is True


class TestHandoffContextFindings:
    """Findings and risks logic."""

    def test_findings__decisions_counted(self):
        ctx = handoff_context("task", decisions=["d1", "d2"])
        assert any("2 decision" in f for f in ctx["findings"])

    def test_findings__evidence_pass_rate(self):
        ctx = handoff_context(
            "task",
            evidence_chain=[
                {"tool": "t1", "summary": "s1", "status": "pass"},
                {"tool": "t2", "summary": "s2", "status": "failed"},
            ],
        )
        assert any("1/2" in f for f in ctx["findings"])

    def test_risks__blockers_added(self):
        ctx = handoff_context("task", blockers=["Schema not ready"])
        assert any("BLOCKER" in r for r in ctx["risks"])

    def test_risks__open_questions_added(self):
        ctx = handoff_context("task", open_questions=["Which table?"])
        assert any("OPEN" in r for r in ctx["risks"])

    def test_risks__no_next_action_warning(self):
        ctx = handoff_context("task")
        assert any("No explicit next action" in r for r in ctx["risks"])

    def test_no_risk__when_next_action_present_no_blockers(self):
        ctx = handoff_context("task", next_action="proceed")
        assert not any("No explicit next action" in r for r in ctx["risks"])


class TestHandoffContextContinuation:
    """continuation dict structure."""

    def test_continuation__stores_next_action(self):
        ctx = handoff_context("task", next_action="Run validate")
        assert ctx["continuation"]["next_action"] == "Run validate"

    def test_continuation__stores_context_needed(self):
        ctx = handoff_context("task", context_needed=["schema", "history"])
        assert ctx["continuation"]["context_needed"] == ["schema", "history"]

    def test_continuation__stores_skip(self):
        ctx = handoff_context("task", skip=["profiling done"])
        assert ctx["continuation"]["skip"] == ["profiling done"]


class TestHandoffContextSummary:
    """Summary string generation."""

    def test_summary__contains_state(self):
        ctx = handoff_context("task", state="blocked")
        assert "blocked" in ctx["summary"]

    def test_summary__contains_blocker_count(self):
        ctx = handoff_context("task", blockers=["b1", "b2"])
        assert "2 blocker" in ctx["summary"]

    def test_summary__contains_next_action(self):
        ctx = handoff_context("task", next_action="Run tests")
        assert "Run tests" in ctx["summary"]


class TestRenderHandoffReport:
    """Tests for markdown rendering."""

    def test_render__returns_string(self):
        ctx = handoff_context("Build pipeline", state="in_progress")
        md = render_handoff_report(ctx)
        assert isinstance(md, str)
        assert "Build pipeline" in md

    def test_output_format_markdown__returns_string(self):
        result = handoff_context("task", output_format="markdown")
        assert isinstance(result, str)
        assert "task" in result

    def test_render__missing_keys_raises(self):
        with pytest.raises(ValueError, match="Missing required keys"):
            render_handoff_report({"kind": "handoff_context"})
