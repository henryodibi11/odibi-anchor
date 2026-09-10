"""Test that all tools comply with the standard output contract.

This test file validates:
1. Every tool returns all STANDARD_CONTRACT keys
2. samples is always a dict (never list)
3. metrics is always a dict
4. findings is always a list
5. risks is always a list
6. suggested_next_actions is always a list of strings
7. kind and summary are non-empty strings

Run:
    pytest tests/test_output_schema.py -v
"""

import json
import os
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, os.path.join(ROOT, "src"))

# Force clean imports
for key in list(sys.modules.keys()):
    if "odibi_anchor" in key:
        del sys.modules[key]

from odibi_anchor.planning import (
    task_execution_context,
    handoff_context,
)
from odibi_anchor.validation import (
    quality_gate_context,
    validation_summary_context,
    duplicate_key_context,
)
from odibi_anchor.tables import (
    diff_tables_by_key,
    schema_diff_context,
    table_contract_summary,
)
from odibi_anchor.profiling import (
    dogfood_regression_context,
)
from odibi_anchor.debugging import error_trace_context
from odibi_anchor.codebase import (
    codebase_map_context,
    change_impact_context,
    session_snapshot_context,
    consistency_check_context,
    convention_preflight_context,
    test_focus_context as _test_focus_context,
    workflow_gate_context,
    framework_lookup_context,
)

# ---------------------------------------------------------------------------
# Standard contract definition
# ---------------------------------------------------------------------------

STANDARD_CONTRACT = {
    "kind", "subject", "summary", "metrics",
    "findings", "risks", "samples", "suggested_next_actions",
}

# ---------------------------------------------------------------------------
# Synthetic data
# ---------------------------------------------------------------------------

_DF = pd.DataFrame({
    "id": [1, 2, 3, 4, 5],
    "name": ["Alice", "Bob", "Charlie", "Dave", None],
    "amount": [100.0, 200.5, None, 400.0, 500.0],
    "date": ["2025-01-01", "2025-01-02", "2025-01-03", "2025-01-04", "2025-01-05"],
    "category": ["A", "B", "A", "C", "B"],
})

_DF2 = pd.DataFrame({
    "id": [1, 2, 3, 4, 6],
    "name": ["Alice", "Bobby", "Charlie", "Dave", "Eve"],
    "amount": [100.0, 250.0, 300.0, 400.0, 600.0],
    "date": ["2025-01-01", "2025-01-02", "2025-01-03", "2025-01-04", "2025-01-06"],
    "category": ["A", "B", "A", "C", "A"],
})

_DF_DUPES = pd.DataFrame({
    "id": [1, 1, 2, 3, 3],
    "value": [10, 20, 30, 40, 50],
})

_FRAMEWORK_ROOT = os.environ.get("FRAMEWORK_ROOT", "")

# ---------------------------------------------------------------------------
# Parametrized tool invocations
# ---------------------------------------------------------------------------


def _call_tool(name: str) -> dict:
    """Call a tool by name with synthetic data. Returns the output dict."""
    calls = {
        "task_execution_context": lambda: task_execution_context(
            task="Schema test", mode="testing",
        ),
        "handoff_context": lambda: handoff_context(
            task="Test", state="in_progress", next_action="continue",
        ),
        "codebase_map_context": lambda: codebase_map_context(root=ROOT),
        "change_impact_context": lambda: change_impact_context(
            root=ROOT,
            target="src/odibi_anchor/planning/task_execution_context.py",
            function="task_execution_context",
            change_type="add_parameter",
        ),
        "session_snapshot_context": lambda: session_snapshot_context(
            root=ROOT, decisions=["test"],
        ),
        "consistency_check_context": lambda: consistency_check_context(root=ROOT),
        "convention_preflight_context": lambda: convention_preflight_context(
            root=ROOT, action="new_tool", function_name="example_context",
        ),
        "test_focus_context": lambda: _test_focus_context(
            root=ROOT,
            changed_files=["src/odibi_anchor/planning/task_execution_context.py"],
        ),
        "workflow_gate_context": lambda: workflow_gate_context(
            root=ROOT, actions_taken=["implement"], files_changed=["src/foo.py"],
        ),
        "framework_lookup_context": lambda: framework_lookup_context(
            "deduplicate", framework_root=_FRAMEWORK_ROOT,
        ),
        "dogfood_regression_context": lambda: dogfood_regression_context(
            current_output={"kind": "test", "metrics": {}}, subject="test", root=ROOT,
        ),
        "quality_gate_context": lambda: quality_gate_context(df=_DF_DUPES, keys=["id"]),
        "validation_summary_context": lambda: validation_summary_context(
            df=_DF, rules=[{"column": "id", "rule": "not_null"}],
        ),
        "duplicate_key_context": lambda: duplicate_key_context(df=_DF_DUPES, keys=["id"]),
        "diff_tables_by_key": lambda: diff_tables_by_key(
            old_df=_DF, new_df=_DF2, keys=["id"],
        ),
        "schema_diff_context": lambda: schema_diff_context(
            old_df=_DF, new_df=_DF2, subject="compare",
        ),
        "table_contract_summary": lambda: table_contract_summary(df=_DF, subject="test"),
        "error_trace_context": lambda: error_trace_context(
            error_text="ValueError: bad value\n  File 'x.py', line 1",
        ),
    }
    return calls[name]()


# All tools that return dict (excludes quick_context which returns str)
ALL_TOOLS = [
    "task_execution_context",
    "handoff_context",
    "codebase_map_context",
    "change_impact_context",
    "session_snapshot_context",
    "consistency_check_context",
    "convention_preflight_context",
    "test_focus_context",
    "workflow_gate_context",
    "dogfood_regression_context",
    "quality_gate_context",
    "validation_summary_context",
    "duplicate_key_context",
    "diff_tables_by_key",
    "schema_diff_context",
    "table_contract_summary",
    "error_trace_context",
]

# Conditionally include an explicitly configured framework integration.
if os.path.isdir(_FRAMEWORK_ROOT):
    ALL_TOOLS.append("framework_lookup_context")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.fixture(params=ALL_TOOLS)
def tool_output(request):
    """Parametrized fixture — calls each tool and returns (name, output)."""
    name = request.param
    output = _call_tool(name)
    return name, output


class TestStandardContract:
    """Validate standard contract compliance across all tools."""

    def test_returns_dict(self, tool_output):
        name, output = tool_output
        assert isinstance(output, dict), f"{name} must return dict, got {type(output)}"

    def test_has_all_contract_keys(self, tool_output):
        name, output = tool_output
        missing = STANDARD_CONTRACT - set(output.keys())
        assert not missing, f"{name} missing contract keys: {sorted(missing)}"

    def test_kind_is_nonempty_str(self, tool_output):
        name, output = tool_output
        assert isinstance(output["kind"], str), f"{name}: kind must be str"
        assert output["kind"], f"{name}: kind must not be empty"

    def test_summary_is_nonempty_str(self, tool_output):
        name, output = tool_output
        assert isinstance(output["summary"], str), f"{name}: summary must be str"
        assert output["summary"], f"{name}: summary must not be empty"

    def test_subject_is_str(self, tool_output):
        name, output = tool_output
        assert isinstance(output["subject"], str), f"{name}: subject must be str"

    def test_metrics_is_dict(self, tool_output):
        name, output = tool_output
        assert isinstance(output["metrics"], dict), (
            f"{name}: metrics must be dict, got {type(output['metrics'])}"
        )

    def test_findings_is_list(self, tool_output):
        name, output = tool_output
        assert isinstance(output["findings"], list), (
            f"{name}: findings must be list, got {type(output['findings'])}"
        )

    def test_risks_is_list(self, tool_output):
        name, output = tool_output
        assert isinstance(output["risks"], list), (
            f"{name}: risks must be list, got {type(output['risks'])}"
        )

    def test_samples_is_dict(self, tool_output):
        name, output = tool_output
        assert isinstance(output["samples"], dict), (
            f"{name}: samples must be dict, got {type(output['samples'])}"
        )

    def test_samples_values_are_lists(self, tool_output):
        """When samples has keys, each value must be a list."""
        name, output = tool_output
        for key, value in output["samples"].items():
            assert isinstance(value, list), (
                f"{name}: samples[\"{key}\"] must be list, got {type(value)}"
            )

    def test_suggested_next_actions_is_list_of_str(self, tool_output):
        name, output = tool_output
        actions = output["suggested_next_actions"]
        assert isinstance(actions, list), (
            f"{name}: suggested_next_actions must be list"
        )
        for item in actions:
            assert isinstance(item, str), (
                f"{name}: suggested_next_actions items must be str, got {type(item)}"
            )

    def test_task_assurance_plan_is_additive_and_json_compatible(self):
        from odibi_anchor.assurance import build_assurance_plan
        from odibi_anchor.planning._task_profile import normalize_task_profile
        from odibi_anchor.planning.task_execution_context import (
            task_execution_context as current_task_execution_context,
        )

        profile = normalize_task_profile(
            legacy_mode="implementation",
            execution_mode="source_change",
        )
        output = current_task_execution_context(
            "Implement a bounded source change",
            goal="Preserve existing contracts",
            acceptance_criteria=["Focused tests pass"],
            _task_profile=profile,
            _assurance_plan=build_assurance_plan(profile),
        )

        assert isinstance(output, dict)
        assert STANDARD_CONTRACT.issubset(output)
        assert output["task_profile"]["schema_version"] == "1.0"
        assert set(output["task_profile"]) == {
            "schema_version", "work_type", "execution_mode", "risk", "rigor",
            "domains", "traits", "caller_required_evidence", "legacy_mode",
            "normalization_notes",
        }
        assert set(output["assurance"]) == {"plan"}
        assert json.loads(json.dumps(output["assurance"])) == output["assurance"]


class TestSamplesContract:
    """Deep validation of the samples dict contract."""

    def test_empty_samples_is_empty_dict(self):
        """Tools with no sample data must return {} not {'key': []}."""
        # These tools never have samples
        no_sample_tools = [
            "session_snapshot_context",
            "workflow_gate_context",
            "handoff_context",
        ]
        for name in no_sample_tools:
            output = _call_tool(name)
            assert output["samples"] == {}, (
                f"{name}: empty samples must be {{}} not {output['samples']}"
            )

    def test_data_tools_wrap_samples_with_semantic_key(self):
        """Data tools wrap sample rows in a semantic key like 'data_preview'."""
        tools_with_samples = [
            ("duplicate_key_context", "duplicate_keys"),
        ]
        for name, expected_key in tools_with_samples:
            output = _call_tool(name)
            if output["samples"]:
                assert expected_key in output["samples"], (
                    f"{name}: expected key \"{expected_key}\" in samples, "
                    f"got {list(output['samples'].keys())}"
                )
