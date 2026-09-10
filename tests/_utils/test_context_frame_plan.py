"""Tests for PlanContext in ContextFrame (TASK_DEEPEN Addition 3)."""
import pytest
from datetime import datetime, timezone

from odibi_anchor._utils._context_frame import (
    ContextFrame,
    PlanContext,
)


class TestPlanContext:
    """Test PlanContext dataclass and its integration with ContextFrame."""

    def test_plan_context_created_on_task_record(self):
        """Recording a task result should populate plan_context on the frame."""
        frame = ContextFrame.new(project="test")

        # Simulate a task result dict
        task_result = {
            "kind": "task_execution_context",
            "subject": "implement feature X",
            "mode": "implementation",
            "readiness": {"score": 85, "passed": True},
            "risks": {"items": ["Risk A", "Risk B"]},
            "verification": {"acceptance_criteria": ["Tests pass", "No regressions"]},
            "findings": [],
        }

        frame.record("task", task_result)

        assert frame.plan_context is not None
        assert frame.plan_context.task == "implement feature X"
        assert frame.plan_context.mode == "implementation"
        assert frame.plan_context.readiness_score == 85
        assert frame.plan_context.risks == ["Risk A", "Risk B"]
        assert frame.plan_context.acceptance_criteria == ["Tests pass", "No regressions"]
        assert frame.plan_context.recorded_at != ""

    def test_plan_context_none_before_task(self):
        """plan_context should be None before any task is recorded."""
        frame = ContextFrame.new(project="test")
        assert frame.plan_context is None

    def test_plan_context_serialization_roundtrip(self):
        """PlanContext should survive to_dict/from_dict serialization."""
        pc = PlanContext(
            task="test task",
            mode="debugging",
            readiness_score=70,
            risks=["risk1"],
            acceptance_criteria=["criteria1"],
            recorded_at="2026-05-28T12:00:00+00:00",
        )

        d = pc.to_dict()
        restored = PlanContext.from_dict(d)

        assert restored.task == "test task"
        assert restored.mode == "debugging"
        assert restored.readiness_score == 70
        assert restored.risks == ["risk1"]
        assert restored.acceptance_criteria == ["criteria1"]

    def test_frame_to_dict_includes_plan_context(self):
        """ContextFrame.to_dict() should include plan_context when set."""
        frame = ContextFrame.new(project="test")
        frame.plan_context = PlanContext(
            task="my task", mode="implementation", readiness_score=100,
            risks=[], acceptance_criteria=["done"], recorded_at="now",
        )

        d = frame.to_dict()
        assert "plan_context" in d
        assert d["plan_context"]["task"] == "my task"
        assert d["plan_context"]["readiness_score"] == 100

    def test_frame_to_dict_plan_context_none(self):
        """ContextFrame.to_dict() should have plan_context=None when not set."""
        frame = ContextFrame.new(project="test")
        d = frame.to_dict()
        assert d["plan_context"] is None

    def test_frame_from_dict_restores_plan_context(self):
        """ContextFrame.from_dict() should restore plan_context."""
        frame = ContextFrame.new(project="test")
        frame.plan_context = PlanContext(
            task="roundtrip task", mode="testing", readiness_score=90,
            risks=["r1"], acceptance_criteria=["ac1"], recorded_at="2026-01-01",
        )

        d = frame.to_dict()
        restored = ContextFrame.from_dict(d)

        assert restored.plan_context is not None
        assert restored.plan_context.task == "roundtrip task"
        assert restored.plan_context.readiness_score == 90
