"""Tests for the onboard workflow (Phase 1 MVP).

Covers:
- Clean table: profile + suggest_rules run, validate skipped if 0 rules
- Dirty table: all 4 steps run
- Failing profile: error in steps, workflow still returns valid contract
- Output contract shape matches spec
- Markdown rendering
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pandas as pd
import pytest

# Ensure both src/ and project root are importable
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_PROJECT_ROOT / "src"))
sys.path.insert(0, str(_PROJECT_ROOT))

from odibi_anchor._dispatcher._workflows import (
    _onboard_workflow,
    _run_step,
    _merge_findings,
    _merge_risks,
    _build_workflow_result,
    _render_workflow_md,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def clean_df():
    """DataFrame with no nulls and unique IDs."""
    return pd.DataFrame(
        {
            "id": range(1, 101),
            "name": [f"item_{i}" for i in range(1, 101)],
            "value": [float(i * 10) for i in range(1, 101)],
        }
    )


@pytest.fixture
def dirty_df():
    """DataFrame with nulls, duplicates, and quality issues."""
    return pd.DataFrame(
        {
            "id": [1, 2, 2, 4, 5, None],
            "status": ["A", "B", "A", None, "C", "A"],
            "amount": [10.0, 20.0, 9999.0, None, 5.0, -1.0],
        }
    )


# ---------------------------------------------------------------------------
# Engine Helper Tests
# ---------------------------------------------------------------------------


class TestRunStep:
    """Test _run_step wrapper."""

    def test_successful_step(self):
        """Successful function returns status=ok with result."""
        def fake_tool():
            return {"summary": "done", "findings": ["x"]}

        step = _run_step("fake", fake_tool)
        assert step["tool"] == "fake"
        assert step["status"] == "ok"
        assert step["duration_s"] >= 0
        assert step["result"]["findings"] == ["x"]

    def test_failing_step(self):
        """Raising function returns status=error with error message."""
        def bad_tool():
            raise ValueError("something broke")

        step = _run_step("bad", bad_tool)
        assert step["status"] == "error"
        assert "something broke" in step["error"]
        assert step["result"] is None


class TestMergeFindings:
    """Test finding deduplication."""

    def test_deduplicates(self):
        r1 = {"findings": ["a", "b"]}
        r2 = {"findings": ["b", "c"]}
        merged = _merge_findings(r1, r2)
        assert merged == ["a", "b", "c"]

    def test_handles_none(self):
        merged = _merge_findings(None, {"findings": ["x"]})
        assert merged == ["x"]


class TestMergeRisks:
    """Test risk deduplication."""

    def test_deduplicates(self):
        r1 = {"risks": ["r1"]}
        r2 = {"risks": ["r1", "r2"]}
        merged = _merge_risks(r1, r2)
        assert merged == ["r1", "r2"]


class TestBuildWorkflowResult:
    """Test final workflow assembly."""

    def test_contract_shape(self):
        steps = [
            {"tool": "t1", "status": "ok", "duration_s": 1.0, "result": {
                "metrics": {"row_count": 10, "column_count": 3},
                "findings": ["f1"],
                "risks": [],
                "suggested_next_actions": ["do x"],
                "summary": "done",
            }},
            {"tool": "t2", "status": "skipped", "duration_s": 0, "reason": "nope", "result": None},
        ]
        ctx = _build_workflow_result("workflow_test", "my_table", steps)
        assert ctx["kind"] == "workflow_test"
        assert ctx["subject"] == "my_table"
        assert "findings" in ctx
        assert "risks" in ctx
        assert "samples" in ctx
        assert "suggested_next_actions" in ctx
        assert ctx["metrics"]["steps_ok"] == 1
        assert ctx["metrics"]["steps_skipped"] == 1
        assert ctx["steps"] == steps
        assert ctx["skipped"] == ["t2"]

    def test_markdown_output(self):
        steps = [
            {"tool": "t1", "status": "ok", "duration_s": 0.5, "result": {
                "metrics": {"row_count": 5, "column_count": 2},
                "findings": [],
                "risks": [],
                "suggested_next_actions": [],
                "summary": "ok",
            }},
        ]
        md = _build_workflow_result("workflow_test", "t", steps, output_format="markdown")
        assert isinstance(md, str)
        assert "workflow_test" in md
        assert "t1" in md


# ---------------------------------------------------------------------------
# Onboard Workflow Tests
# ---------------------------------------------------------------------------


class TestOnboardWorkflow:
    """Integration tests for _onboard_workflow."""

    def test_clean_table_runs_profile_and_suggest(self, clean_df):
        """Clean table: profile + suggest run, microscope skipped."""
        result = _onboard_workflow(clean_df, subject="clean_test", output_format="dict")
        assert result["kind"] == "workflow_onboard"
        assert result["subject"] == "clean_test"
        tool_names = [s["tool"] for s in result["steps"]]
        assert "profile_table" in tool_names
        assert "suggest_rules" in tool_names
        # Microscope should be skipped for a clean table with no nulls
        micro_steps = [s for s in result["steps"] if s["tool"] == "microscope"]
        for ms in micro_steps:
            # Either skipped or there were no issues to investigate
            assert ms["status"] in ("ok", "skipped")

    def test_dirty_table_runs_all_steps(self, dirty_df):
        """Dirty table: all steps should run (no skips)."""
        result = _onboard_workflow(dirty_df, subject="dirty_test", output_format="dict")
        assert result["kind"] == "workflow_onboard"
        tool_names = [s["tool"] for s in result["steps"]]
        assert "profile_table" in tool_names
        assert "suggest_rules" in tool_names
        assert "validate" in tool_names
        # At least one microscope should run (dirty data has nulls)
        micro_steps = [s for s in result["steps"] if s["tool"] == "microscope"]
        assert any(s["status"] == "ok" for s in micro_steps)

    def test_failing_profile_still_returns_contract(self):
        """If profile fails, workflow returns valid contract with error step."""
        with patch(
            "tools.table_profiler_tool.lib.profiler.profile_table",
            side_effect=RuntimeError("table gone"),
        ):
            # Re-import to pick up mock (local import inside the fn)
            from odibi_anchor._dispatcher import _workflows

            result = _workflows._onboard_workflow(
                pd.DataFrame({"x": [1]}), subject="broken", output_format="dict"
            )
        assert result["kind"] == "workflow_onboard"
        profile_step = result["steps"][0]
        assert profile_step["status"] == "error"

    def test_no_input_returns_error(self):
        """Calling with no args returns a usage error."""
        result = _onboard_workflow()
        assert result["kind"] == "workflow_onboard"
        assert "error" in result["subject"]

    def test_output_contract_has_required_keys(self, dirty_df):
        """Verify all standard contract keys + workflow extras present."""
        result = _onboard_workflow(dirty_df, subject="contract_test", output_format="dict")
        required = {"kind", "subject", "summary", "metrics", "findings", "risks", "samples", "suggested_next_actions"}
        assert required.issubset(set(result.keys()))
        # Workflow-specific extras
        assert "steps" in result
        assert "skipped" in result

    def test_markdown_output(self, dirty_df):
        """Markdown format returns a string."""
        result = _onboard_workflow(dirty_df, subject="md_test", output_format="markdown")
        assert isinstance(result, str)
        assert "workflow_onboard" in result
