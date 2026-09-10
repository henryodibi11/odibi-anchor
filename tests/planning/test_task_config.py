"""Tests for odibi_anchor.planning._task_config — structure and content validation."""

import pytest

from odibi_anchor.planning._task_config import (
    _READINESS_GAP_QUESTIONS,
    _MODE_HINTS,
    _MODE_DEFAULTS,
    _MODE_EVIDENCE_GAPS,
)


class TestReadinessGapQuestions:
    """Validate _READINESS_GAP_QUESTIONS structure."""

    def test_is_dict(self):
        assert isinstance(_READINESS_GAP_QUESTIONS, dict)

    def test_all_values_are_lists_of_strings(self):
        for key, questions in _READINESS_GAP_QUESTIONS.items():
            assert isinstance(questions, list), f"Value for {key!r} is not a list"
            for q in questions:
                assert isinstance(q, str), f"Question in {key!r} is not a string: {q!r}"

    def test_expected_keys_present(self):
        expected = [
            "Goal or desired outcome is missing.",
            "Acceptance criteria are missing.",
            "Constraints are missing.",
        ]
        for key in expected:
            assert key in _READINESS_GAP_QUESTIONS, f"Missing key: {key!r}"

    def test_each_key_has_at_least_one_question(self):
        for key, questions in _READINESS_GAP_QUESTIONS.items():
            assert len(questions) >= 1, f"Key {key!r} has no questions"


class TestModeHints:
    """Validate _MODE_HINTS structure."""

    EXPECTED_MODES = {"planning", "implementation", "testing", "debugging", "review", "migration", "handoff", "decision", "analysis", "retrospective"}
    EXPECTED_KEYS = {"thinking_prompts", "prompting_hints", "execution_hints", "anti_patterns"}

    def test_is_dict(self):
        assert isinstance(_MODE_HINTS, dict)

    def test_covers_all_expected_modes(self):
        for mode in self.EXPECTED_MODES:
            assert mode in _MODE_HINTS, f"Mode {mode!r} missing from _MODE_HINTS"

    def test_each_mode_has_all_hint_categories(self):
        for mode, hints in _MODE_HINTS.items():
            for key in self.EXPECTED_KEYS:
                assert key in hints, f"Mode {mode!r} missing key {key!r}"
                assert isinstance(hints[key], list)
                assert len(hints[key]) >= 1, f"Mode {mode!r} key {key!r} is empty"

    def test_all_hints_are_strings(self):
        for mode, hints in _MODE_HINTS.items():
            for key, values in hints.items():
                for v in values:
                    assert isinstance(v, str), f"Non-string in {mode}.{key}: {v!r}"


class TestModeDefaults:
    """Validate _MODE_DEFAULTS structure."""

    EXPECTED_KEYS = {"plan", "critique_checks", "verification_steps", "suggested_next_actions"}

    def test_is_dict(self):
        assert isinstance(_MODE_DEFAULTS, dict)

    def test_covers_same_modes_as_hints(self):
        # MODE_DEFAULTS should cover at least testing, implementation, planning
        assert "planning" in _MODE_DEFAULTS
        assert "implementation" in _MODE_DEFAULTS
        assert "testing" in _MODE_DEFAULTS

    def test_each_mode_has_plan(self):
        for mode, defaults in _MODE_DEFAULTS.items():
            assert "plan" in defaults
            assert isinstance(defaults["plan"], list)
            assert len(defaults["plan"]) >= 1

    def test_plan_steps_have_required_fields(self):
        for mode, defaults in _MODE_DEFAULTS.items():
            for step in defaults["plan"]:
                assert "phase" in step, f"Step in {mode} plan missing 'phase'"
                assert "action" in step, f"Step in {mode} plan missing 'action'"
                assert "done_when" in step, f"Step in {mode} plan missing 'done_when'"

    def test_critique_checks_are_strings(self):
        for mode, defaults in _MODE_DEFAULTS.items():
            if "critique_checks" in defaults:
                for check in defaults["critique_checks"]:
                    assert isinstance(check, str)


class TestModeEvidenceGaps:
    """Validate _MODE_EVIDENCE_GAPS structure."""

    def test_is_dict(self):
        assert isinstance(_MODE_EVIDENCE_GAPS, dict)

    def test_values_are_lists_of_strings(self):
        for mode, gaps in _MODE_EVIDENCE_GAPS.items():
            assert isinstance(gaps, list)
            for gap in gaps:
                assert isinstance(gap, str)

    def test_testing_mode_has_gaps(self):
        assert "testing" in _MODE_EVIDENCE_GAPS
        assert len(_MODE_EVIDENCE_GAPS["testing"]) >= 1
