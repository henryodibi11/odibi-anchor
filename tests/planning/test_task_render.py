"""Tests for odibi_anchor.planning._task_render."""

import pytest

from odibi_anchor.planning._task_render import (
    render_task_execution_report,
    _render_prompt_brief,
    _render_teammate_brief,
    _format_context_generator,
    _append_context_generators,
    _append_text,
    _append_list,
    _append_records,
    _append_discovery_steps,
    _append_plan,
    _append_resources,
)


def _minimal_context(**overrides):
    """Minimal valid context dict for render functions."""
    ctx = {
        "kind": "task_execution_context",
        "subject": "Test task",
        "summary": "Testing rendering",
        "status": "READY",
        "mode": "testing",
        "audience": "agent",
        "readiness": {"status": "ready", "score": 80},
        "intent": {"task": "Test it", "goal": "Verify output"},
        "background": {"current_state": None, "summary": None},
        "scope": {"in_scope": [], "out_of_scope": []},
        "resources": {"artifacts": [], "inputs": [], "dependencies": []},
        "constraints": [],
        "context": {"open_questions": [], "decisions_needed": [], "evidence": [], "options": []},
        "discovery": {"evidence_gaps": [], "recommended_discovery_steps": [], "recommended_context_generators": []},
        "plan": [],
        "critique_checks": [],
        "verification": {"stop_conditions": [], "acceptance_criteria": [], "deliverables": [], "expected_output_format": None},
        "hints": {"thinking_prompts": [], "prompting_hints": [], "clarifying_questions": []},
        "ownership": {},
        "risks": [],
        "suggested_next_actions": [],
    }
    ctx.update(overrides)
    return ctx


# ─── render_task_execution_report ─────────────────────────────────────────────


class TestRenderTaskExecutionReport:
    """Tests for the main public render function."""

    def test_returns_string(self):
        ctx = _minimal_context()
        result = render_task_execution_report(ctx)
        assert isinstance(result, str)

    def test_contains_subject(self):
        ctx = _minimal_context(subject="My Task")
        result = render_task_execution_report(ctx)
        assert "My Task" in result

    def test_contains_status(self):
        ctx = _minimal_context(status="NEEDS_CLARIFICATION")
        result = render_task_execution_report(ctx)
        assert "NEEDS_CLARIFICATION" in result

    def test_contains_summary(self):
        ctx = _minimal_context(summary="Build the thing")
        result = render_task_execution_report(ctx)
        assert "Build the thing" in result

    def test_contains_readiness_score(self):
        ctx = _minimal_context()
        result = render_task_execution_report(ctx)
        assert "80/100" in result

    def test_plan_rendered(self):
        ctx = _minimal_context(plan=[
            {"step": 1, "phase": "orient", "action": "Check files", "done_when": "Files known"},
        ])
        result = render_task_execution_report(ctx)
        assert "orient" in result
        assert "Check files" in result

    def test_evidence_gaps_rendered(self):
        ctx = _minimal_context(discovery={"evidence_gaps": ["Need schema info"], "recommended_discovery_steps": [], "recommended_context_generators": []})
        result = render_task_execution_report(ctx)
        assert "Need schema info" in result

    def test_missing_required_keys__raises_value_error(self):
        with pytest.raises(ValueError, match="Missing required keys"):
            render_task_execution_report({"kind": "task_execution_context"})

    def test_missing_kind__raises(self):
        with pytest.raises(ValueError):
            render_task_execution_report({"subject": "x", "summary": "y", "status": "z"})


# ─── _render_prompt_brief ─────────────────────────────────────────────────────


class TestRenderPromptBrief:
    """Tests for agent-facing prompt brief."""

    def test_contains_mode(self):
        ctx = _minimal_context(mode="testing")
        result = _render_prompt_brief(ctx)
        assert "testing" in result.lower()

    def test_contains_readiness(self):
        ctx = _minimal_context()
        result = _render_prompt_brief(ctx)
        assert "80/100" in result

    def test_agent_audience_has_execution_instruction(self):
        ctx = _minimal_context(audience="agent")
        result = _render_prompt_brief(ctx)
        assert "Execution instruction" in result

    def test_non_agent_audience_no_execution_instruction(self):
        ctx = _minimal_context(audience="teammate")
        result = _render_prompt_brief(ctx)
        assert "Execution instruction" not in result


# ─── _render_teammate_brief ───────────────────────────────────────────────────


class TestRenderTeammateBrief:
    """Tests for teammate-facing brief."""

    def test_contains_subject(self):
        ctx = _minimal_context(subject="Data pipeline")
        result = _render_teammate_brief(ctx)
        assert "Data pipeline" in result

    def test_contains_summary(self):
        ctx = _minimal_context(summary="Short description")
        result = _render_teammate_brief(ctx)
        assert "Short description" in result


# ─── Helper functions ─────────────────────────────────────────────────────────


class TestFormatContextGenerator:
    """Tests for _format_context_generator."""

    def test_name_only(self):
        assert _format_context_generator({"name": "profile"}) == "profile"

    def test_name_with_status(self):
        result = _format_context_generator({"name": "profile", "status": "available"})
        assert result == "profile (available)"

    def test_missing_name_shows_unknown(self):
        result = _format_context_generator({})
        assert result == "unknown"


class TestAppendHelpers:
    """Tests for _append_* helper functions."""

    def test_append_text__non_empty(self):
        lines = []
        _append_text(lines, "Title", "content here")
        assert "Title:" in lines
        assert "content here" in lines

    def test_append_text__none_skips(self):
        lines = []
        _append_text(lines, "Title", None)
        assert len(lines) == 0

    def test_append_list__non_empty(self):
        lines = []
        _append_list(lines, "Items", ["a", "b"])
        assert "Items:" in lines
        assert "- a" in lines

    def test_append_list__empty_skips(self):
        lines = []
        _append_list(lines, "Items", [])
        assert len(lines) == 0

    def test_append_records__extracts_name(self):
        lines = []
        _append_records(lines, "Records", [{"name": "rec1", "role": "source"}])
        assert any("rec1" in l for l in lines)

    def test_append_records__empty_skips(self):
        lines = []
        _append_records(lines, "Records", [])
        assert len(lines) == 0

    def test_append_plan__numbered_steps(self):
        lines = []
        _append_plan(lines, [{"step": 1, "phase": "orient", "action": "Check", "done_when": "Done"}])
        assert any("orient" in l for l in lines)
        assert any("Done when" in l for l in lines)

    def test_append_discovery_steps__with_tool(self):
        lines = []
        _append_discovery_steps(lines, [{"step": 1, "action": "Run profile", "suggested_tool": "profile_table", "why": "need data"}])
        assert any("profile_table" in l for l in lines)

    def test_append_resources__artifacts(self):
        lines = []
        _append_resources(lines, {"artifacts": [{"name": "file.py", "role": "source"}], "inputs": [], "dependencies": []})
        assert any("file.py" in l for l in lines)

    def test_append_resources__empty_skips(self):
        lines = []
        _append_resources(lines, {"artifacts": [], "inputs": [], "dependencies": []})
        assert len(lines) == 0

    def test_append_context_generators__non_empty(self):
        lines = []
        _append_context_generators(lines, "Generators", [{"name": "profile", "status": "available", "reason": "needed"}])
        assert any("profile" in l for l in lines)
        assert any("needed" in l for l in lines)
