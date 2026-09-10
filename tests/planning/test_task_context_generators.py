"""Tests for already_called filter in _recommend_context_generators (TASK_DEEPEN Addition 2)."""
from odibi_anchor.planning._task_profile import normalize_task_profile
from odibi_anchor.planning.context_selection import select_context

# Import internal function for direct testing
from odibi_anchor.planning.task_execution_context import (
    _recommend_context_generators,
    task_execution_context,
)


def _recommend(task, mode, already_called=None):
    plan = select_context(
        normalize_task_profile(legacy_mode=mode, task_text=task),
        task_text=task,
        already_called=already_called,
    )
    return _recommend_context_generators(context_plan=plan)


class TestAlreadyCalledFilter:
    """Test the already_called filtering in _recommend_context_generators."""

    def test_already_called_filters_matching_tools(self):
        """Tools in already_called should be reported without claiming evidence is complete."""
        # Use a task that will trigger some recommendations
        result = _recommend(
            "validate data quality my_table", "testing",
            already_called=["validate", "quality"],
        )

        # Find entries that were reported as already called.
        already_called = [r for r in result if r["status"] == "already_called"]
        assert len(already_called) > 0
        # Verify the ones we passed are marked
        already_called_names = {r["name"] for r in already_called}
        assert "validate" in already_called_names

    def test_already_called_shows_correct_status(self):
        """Already-called entries should not be treated as satisfied evidence."""
        result = _recommend(
            "check schema drift test", "implementation",
            already_called=["schema_diff"],
        )

        schema_entries = [r for r in result if r["name"] == "schema_diff"]
        if schema_entries:
            entry = schema_entries[0]
            assert entry["status"] == "already_called"
            assert "supports" in entry["reason"].lower()

    def test_empty_already_called_normal_behaviour(self):
        """Empty/None already_called should produce normal recommendations."""
        result_none = _recommend("validate data table", "testing", already_called=None)
        result_empty = _recommend("validate data table", "testing", already_called=[])

        # No entries should be reported as already called.
        for r in result_none:
            assert r["status"] != "already_called"
        for r in result_empty:
            assert r["status"] != "already_called"

    def test_already_called_param_passed_through_main_function(self):
        """task_execution_context should accept _already_called and pass it through."""
        # This tests the integration: _already_called kwarg should not crash
        result = task_execution_context(
            "test task for context generator filtering",
            goal="verify _already_called integration",
            mode="debugging",
            _already_called=["trace"],
            output_format="dict",
        )

        # Should not crash and should contain discovery section
        assert "discovery" in result
        generators = result["discovery"].get("recommended_context_generators", [])
        trace = next(g for g in generators if g["name"] == "trace")
        assert trace["status"] == "already_called"
