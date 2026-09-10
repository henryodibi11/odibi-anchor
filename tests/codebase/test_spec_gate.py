"""Tests for Phase 3 spec gate criteria verification (_spec.py)."""
from pathlib import Path

import pytest

from odibi_anchor._dispatcher._spec import (
    check_spec_criteria,
    render_spec_criteria,
    _match_criterion,
)


# ---------------------------------------------------------------------------
# _match_criterion heuristic matchers
# ---------------------------------------------------------------------------


class TestMatchCriterionFileExists:
    """File existence matcher."""

    def test_py_file_exists(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "module.py").write_text("# x")
        emoji, check_type, evidence = _match_criterion(
            "src/module.py should exist", [], tmp_path
        )
        assert emoji == "✅"
        assert check_type == "file_exists"
        assert "src/module.py" in evidence

    def test_py_file_missing(self, tmp_path):
        emoji, check_type, _ = _match_criterion(
            "src/missing.py should exist", [], tmp_path
        )
        assert emoji == "⬜"
        assert check_type == "file_missing"

    def test_md_file_exists(self, tmp_path):
        (tmp_path / "README.md").write_text("# Docs")
        emoji, check_type, _ = _match_criterion(
            "README.md is present", [], tmp_path
        )
        assert emoji == "✅"
        assert check_type == "file_exists"


class TestMatchCriterionTestPass:
    """Test pass matcher."""

    def _test_timing(self, action="test", error=None):
        return {"action": action, "error": error, "elapsed_ms": 100}

    def test_passes_when_test_ran(self, tmp_path):
        timings = [self._test_timing("test")]
        emoji, check_type, _ = _match_criterion(
            "all tests pass", timings, tmp_path
        )
        assert emoji == "✅"
        assert check_type == "test_passed"

    def test_pending_when_no_test_ran(self, tmp_path):
        emoji, check_type, _ = _match_criterion(
            "all tests pass", [], tmp_path
        )
        assert emoji == "⬜"
        assert check_type == "test_not_run"

    def test_passes_with_passing_keyword(self, tmp_path):
        # anchor("test") records action "test" (test_focus_context is the builder behind it)
        timings = [self._test_timing("test")]
        emoji, _, _ = _match_criterion(
            "tests are passing", timings, tmp_path
        )
        assert emoji == "✅"

    def test_ignores_failed_test_run(self, tmp_path):
        timings = [self._test_timing("test", error="FAILED")]
        emoji, check_type, _ = _match_criterion(
            "all tests pass", timings, tmp_path
        )
        assert emoji == "⬜"
        assert check_type == "test_not_run"


class TestMatchCriterionCwAction:
    """anchor() action matcher."""

    def _timing(self, action, error=None):
        return {"action": action, "error": error, "elapsed_ms": 50}

    def test_action_ran(self, tmp_path):
        timings = [self._timing("spec")]
        emoji, check_type, evidence = _match_criterion(
            'anchor("spec") lists specs', timings, tmp_path
        )
        assert emoji == "✅"
        assert check_type == "action_ran"
        assert "spec" in evidence

    def test_action_not_run(self, tmp_path):
        emoji, check_type, _ = _match_criterion(
            'anchor("spec") lists specs', [], tmp_path
        )
        assert emoji == "⬜"
        assert check_type == "action_not_run"

    def test_failed_action_not_counted(self, tmp_path):
        timings = [self._timing("spec", error="RuntimeError")]
        emoji, check_type, _ = _match_criterion(
            'anchor("spec") lists specs', timings, tmp_path
        )
        assert emoji == "⬜"
        assert check_type == "action_not_run"


class TestMatchCriterionAgentAttested:
    """Agent attested fallback."""

    def test_unmatched_criterion(self, tmp_path):
        emoji, check_type, evidence = _match_criterion(
            "Code is readable and maintainable", [], tmp_path
        )
        assert emoji == "⬜"
        assert check_type == "agent_attested"
        assert "agent confirmation" in evidence


# ---------------------------------------------------------------------------
# check_spec_criteria integration
# ---------------------------------------------------------------------------


class TestCheckSpecCriteria:
    """Integration tests for check_spec_criteria."""

    def test_returns_spec_name(self, tmp_path):
        spec = {"name": "MY_FEATURE", "success_criteria": []}
        result = check_spec_criteria(spec, [], str(tmp_path))
        assert result["spec_name"] == "MY_FEATURE"
        assert result["criteria"] == []

    def test_empty_criteria(self, tmp_path):
        spec = {"name": "X", "success_criteria": []}
        result = check_spec_criteria(spec, [], str(tmp_path))
        assert result["criteria"] == []

    def test_multiple_criteria_types(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "module.py").write_text("x = 1")
        timings = [{"action": "gate", "error": None, "elapsed_ms": 100}]
        spec = {
            "name": "TEST_SPEC",
            "success_criteria": [
                "src/module.py exists",
                'anchor("gate") ran successfully',
                "Code quality is acceptable",
            ],
        }
        result = check_spec_criteria(spec, timings, str(tmp_path))
        checks = result["criteria"]
        assert len(checks) == 3
        assert checks[0]["check_type"] == "file_exists"
        assert checks[0]["status"] == "✅"
        assert checks[1]["check_type"] == "action_ran"
        assert checks[1]["status"] == "✅"
        assert checks[2]["check_type"] == "agent_attested"
        assert checks[2]["status"] == "⬜"

    def test_missing_success_criteria_key(self, tmp_path):
        spec = {"name": "X"}  # no success_criteria key
        result = check_spec_criteria(spec, [], str(tmp_path))
        assert result["criteria"] == []


# ---------------------------------------------------------------------------
# render_spec_criteria
# ---------------------------------------------------------------------------


class TestRenderSpecCriteria:
    """Tests for markdown rendering."""

    def test_empty_criteria_returns_empty_string(self):
        result = render_spec_criteria({"spec_name": "X", "criteria": []})
        assert result == ""

    def test_renders_section_header(self):
        data = {
            "spec_name": "MY_SPEC",
            "criteria": [
                {"status": "✅", "criterion": "It works", "evidence": "confirmed"},
            ],
        }
        output = render_spec_criteria(data)
        assert "Spec Criteria (MY_SPEC)" in output
        assert "✅ It works" in output
        assert "[confirmed]" in output

    def test_renders_multiple_criteria(self):
        data = {
            "spec_name": "S",
            "criteria": [
                {"status": "✅", "criterion": "Done", "evidence": "e1"},
                {"status": "⬜", "criterion": "Pending", "evidence": "e2"},
            ],
        }
        output = render_spec_criteria(data)
        assert "✅ Done" in output
        assert "⬜ Pending" in output
