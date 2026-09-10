"""Tests for odibi_anchor._dispatcher._frame — contextual suggestions."""

import pytest
from unittest.mock import MagicMock, patch
from types import SimpleNamespace

from odibi_anchor._dispatcher._frame import (
    _build_contextual_suggestions,
    _frame_action,
)


def _make_finding(source="profile_table", message="15% null rate on created_date", subject="table_a", severity="warning"):
    return SimpleNamespace(source=source, message=message, subject=subject, severity=severity)


def _make_risk(severity="high"):
    return SimpleNamespace(severity=severity)


def _make_frame(*, findings=None, risks=None, files_changed=None, has_gotcha=False, has_profile=False):
    """Create a mock ContextFrame."""
    frame = MagicMock()
    frame.findings = findings or []
    frame.risks = risks or []
    frame.code_context.files_changed = files_changed or []
    frame.memory_context.has_gotcha_for = MagicMock(return_value=has_gotcha)
    frame.data_context.has_profile = MagicMock(return_value=has_profile)
    return frame


class TestBuildContextualSuggestions:
    """Tests for _build_contextual_suggestions."""

    def test_empty_frame__no_suggestions(self):
        frame = _make_frame()
        result = {"suggested_next_actions": []}
        suggestions = _build_contextual_suggestions(frame, result)
        assert suggestions == []

    def test_null_anomaly__suggests_microscope(self):
        findings = [_make_finding(source="profile_table", message="15% null rate on created_date")]
        frame = _make_frame(findings=findings)
        result = {"suggested_next_actions": []}
        suggestions = _build_contextual_suggestions(frame, result)
        assert any("microscope" in s for s in suggestions)
        assert any("created_date" in s for s in suggestions)

    def test_freshness_issue__suggests_case_file(self):
        findings = [_make_finding(source="profile_table", message="Table is stale — last update 7 days ago", subject="sales")]
        frame = _make_frame(findings=findings)
        result = {"suggested_next_actions": []}
        suggestions = _build_contextual_suggestions(frame, result)
        assert any("freshness" in s.lower() or "case_file" in s for s in suggestions)

    def test_file_with_gotcha__suggests_memory_review(self):
        frame = _make_frame(files_changed=["src/module.py"], has_gotcha=True)
        result = {"suggested_next_actions": []}
        suggestions = _build_contextual_suggestions(frame, result)
        assert any("gotcha" in s.lower() or "memory" in s.lower() for s in suggestions)

    def test_validation_blocker__suggests_must_fix(self):
        findings = [_make_finding(source="validate", message="not_null failed on id", subject="orders", severity="critical")]
        frame = _make_frame(findings=findings)
        result = {"suggested_next_actions": []}
        suggestions = _build_contextual_suggestions(frame, result)
        assert any("MUST" in s and "validation" in s.lower() for s in suggestions)

    def test_high_risk_accumulation__suggests_gate(self):
        risks = [_make_risk("high") for _ in range(4)]
        frame = _make_frame(risks=risks)
        result = {"suggested_next_actions": []}
        suggestions = _build_contextual_suggestions(frame, result)
        assert any("gate" in s.lower() for s in suggestions)

    def test_deduplication__no_repeat_suggestions(self):
        findings = [_make_finding(source="profile_table", message="15% null rate on col_a")]
        frame = _make_frame(findings=findings)
        # Pre-existing suggestion that matches what would be generated
        existing = "SHOULD: anchor('microscope', df, 'col_a') — profiler found null anomaly"
        result = {"suggested_next_actions": [existing]}
        suggestions = _build_contextual_suggestions(frame, result)
        # Should not duplicate
        assert not any("col_a" in s for s in suggestions)

    def test_already_profiled__suggests_skip(self):
        frame = _make_frame(has_profile=True)
        result = {"suggested_next_actions": [], "subject": "sales_table", "kind": "profile_table"}
        suggestions = _build_contextual_suggestions(frame, result)
        assert any("already profiled" in s.lower() for s in suggestions)

    def test_never_raises(self):
        """Even with broken frame, should not raise."""
        frame = MagicMock()
        frame.findings = None  # This would normally crash iteration
        frame.risks = []
        frame.code_context.files_changed = []
        result = {"suggested_next_actions": []}
        # Should not raise
        suggestions = _build_contextual_suggestions(frame, result)
        assert isinstance(suggestions, list)


class TestFrameAction:
    """Tests for _frame_action."""

    def test_frame_disabled__returns_not_initialized(self):
        result = _frame_action(None, False, output_format="dict")
        assert result["kind"] == "context_frame"
        assert result["metrics"]["initialized"] is False

    def test_frame_none_enabled__returns_not_initialized(self):
        result = _frame_action(None, True, output_format="dict")
        assert result["metrics"]["initialized"] is False
