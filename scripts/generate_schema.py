#!/usr/bin/env python
"""Auto-generate OUTPUT_SCHEMA.md from live tool introspection.

Run this script to regenerate the schema doc whenever tools change.
It imports all context tools, calls each with minimal synthetic data,
and writes a TypedDict-style schema for every tool's output.

Usage:
    python scripts/generate_schema.py

Output:
    OUTPUT_SCHEMA.md in the project root.

Dependencies: pandas (for synthetic DataFrames). No Spark required.
"""

from __future__ import annotations

import inspect
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

# Ensure project source is importable
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import pandas as pd

# Force clean imports
for key in list(sys.modules.keys()):
    if "odibi_anchor" in key:
        del sys.modules[key]

from odibi_anchor.planning import (
    task_execution_context,
    quick_context,
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
from odibi_anchor.debugging import error_trace_context, failure_pattern_context
from odibi_anchor.codebase import (
    codebase_map_context,
    change_impact_context,
    session_snapshot_context,
    consistency_check_context,
    convention_preflight_context,
    test_focus_context,
    workflow_gate_context,
    framework_lookup_context,
    memory_context,
    semantic_edit_context,
    preflight_context,
    import_resolve_context,
    safe_change_context,
    learn_context,
    known_bad_change_context,
)


# ---------------------------------------------------------------------------
# Synthetic test data
# ---------------------------------------------------------------------------

_DF = pd.DataFrame(
    {
        "id": [1, 2, 3, 4, 5],
        "name": ["Alice", "Bob", "Charlie", "Dave", None],
        "amount": [100.0, 200.5, None, 400.0, 500.0],
        "date": ["2025-01-01", "2025-01-02", "2025-01-03", "2025-01-04", "2025-01-05"],
        "category": ["A", "B", "A", "C", "B"],
    }
)

_DF2 = pd.DataFrame(
    {
        "id": [1, 2, 3, 4, 6],
        "name": ["Alice", "Bobby", "Charlie", "Dave", "Eve"],
        "amount": [100.0, 250.0, 300.0, 400.0, 600.0],
        "date": ["2025-01-01", "2025-01-02", "2025-01-03", "2025-01-04", "2025-01-06"],
        "category": ["A", "B", "A", "C", "A"],
    }
)

_DF_DUPES = pd.DataFrame(
    {
        "id": [1, 1, 2, 3, 3],
        "value": [10, 20, 30, 40, 50],
    }
)

_ROOT = str(ROOT)
_FRAMEWORK_ROOT = str(ROOT.parent / "odibi")


# ---------------------------------------------------------------------------
# Tool registry: (name, callable, kwargs)
# ---------------------------------------------------------------------------


def _build_registry() -> list[tuple[str, Any, dict[str, Any] | None]]:
    """Build the list of (tool_name, function, kwargs) to call."""
    return [
        # Planning
        ("task_execution_context", task_execution_context, dict(
            task="Schema test", mode="implementation",
            background="Testing schema generation.",
            constraints=["No side effects"],
            guardrails={"do_not_modify": ["tests/"]},
        )),
        ("handoff_context", handoff_context, dict(
            task="Test handoff", state="in_progress", next_action="continue",
        )),
        # Codebase
        ("codebase_map_context", codebase_map_context, dict(root=_ROOT)),
        ("change_impact_context", change_impact_context, dict(
            root=_ROOT,
            target="src/odibi_anchor/planning/task_execution_context.py",
            function="task_execution_context",
            change_type="add_parameter",
        )),
        ("session_snapshot_context", session_snapshot_context, dict(
            root=_ROOT, decisions=["schema generation"],
        )),
        ("consistency_check_context", consistency_check_context, dict(root=_ROOT)),
        ("convention_preflight_context", convention_preflight_context, dict(
            root=_ROOT, action="new_tool", function_name="example_context",
        )),
        ("test_focus_context", test_focus_context, dict(
            root=_ROOT,
            changed_files=["src/odibi_anchor/planning/task_execution_context.py"],
        )),
        ("workflow_gate_context", workflow_gate_context, dict(
            root=_ROOT, actions_taken=["implement"], files_changed=["src/foo.py"],
        )),
        ("framework_lookup_context", framework_lookup_context, (
            {"query": "deduplicate", "framework_root": _FRAMEWORK_ROOT}
            if os.path.isdir(_FRAMEWORK_ROOT)
            else None  # skip if not available
        )),
        # Profiling
        ("dogfood_regression_context", dogfood_regression_context, dict(
            current_output={"kind": "test", "metrics": {}}, subject="test", root=_ROOT,
        )),
        # Validation
        ("quality_gate_context", quality_gate_context, dict(df=_DF_DUPES, keys=["id"])),
        ("validation_summary_context", validation_summary_context, dict(
            df=_DF, rules=[{"column": "id", "rule": "not_null"}],
        )),
        ("duplicate_key_context", duplicate_key_context, dict(df=_DF_DUPES, keys=["id"])),
        # Tables
        ("diff_tables_by_key", diff_tables_by_key, dict(old_df=_DF, new_df=_DF2, keys=["id"])),
        ("schema_diff_context", schema_diff_context, dict(old_df=_DF, new_df=_DF2, subject="compare")),
        ("table_contract_summary", table_contract_summary, dict(df=_DF, subject="test_data")),
        # Debugging
        ("error_trace_context", error_trace_context, dict(
            error_text="ValueError: invalid literal\n  File \'x.py\', line 1",
        )),
    ]


# ---------------------------------------------------------------------------
# Schema inference
# ---------------------------------------------------------------------------


def _infer_type(value: Any, depth: int = 0) -> str:
    """Infer a TypedDict-style type string from a value."""
    if value is None:
        return "None"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if isinstance(value, list):
        if not value:
            return "list[Any]"
        inner = _infer_type(value[0], depth + 1)
        return f"list[{inner}]"
    if isinstance(value, dict):
        if not value:
            return "dict[str, Any]"
        if depth > 1:
            return "dict[str, Any]"
        return "dict"  # will be expanded in schema
    return type(value).__name__


def _build_schema(output: dict[str, Any]) -> dict[str, str]:
    """Build a {key: type_str} schema from a tool output dict."""
    schema = {}
    for key, value in sorted(output.items()):
        schema[key] = _infer_type(value)
    return schema


def _describe_samples(samples: dict[str, Any]) -> str:
    """Describe the samples dict structure."""
    if not samples:
        return "{} (empty when no sample data)"
    parts = []
    for k, v in sorted(samples.items()):
        if isinstance(v, list):
            if v:
                inner = _infer_type(v[0])
                parts.append(f"\"{k}\": list[{inner}]")
            else:
                parts.append(f"\"{k}\": list (empty)")
        else:
            parts.append(f"\"{k}\": {_infer_type(v)}")
    return "{" + ", ".join(parts) + "}"


def _describe_metrics(metrics: dict[str, Any]) -> list[str]:
    """List metric keys and their types."""
    lines = []
    for k, v in sorted(metrics.items()):
        lines.append(f"  {k}: {_infer_type(v)}")
    return lines


# ---------------------------------------------------------------------------
# Main generation
# ---------------------------------------------------------------------------

STANDARD_CONTRACT = {
    "kind", "subject", "summary", "metrics",
    "findings", "risks", "samples", "suggested_next_actions",
}


def generate_schema(skip_slow: bool = False) -> str:
    """Generate the full OUTPUT_SCHEMA.md content."""
    registry = _build_registry()
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    sections: list[str] = []
    sections.append(f"# OUTPUT_SCHEMA.md")
    sections.append(f"")
    sections.append(f"Auto-generated on {now} by `scripts/generate_schema.py`.")
    sections.append(f"**Do not edit manually** — re-run the script to regenerate.")
    sections.append(f"")
    sections.append(f"## Standard Contract")
    sections.append(f"")
    sections.append(f"Every tool returns a dict with at minimum these keys:")
    sections.append(f"")
    sections.append(f"```python")
    sections.append(f"class StandardContract(TypedDict):")
    sections.append(f"    kind: str                        # Tool identifier")
    sections.append(f"    subject: str                     # Human label for the target")
    sections.append(f"    summary: str                     # One-line human summary")
    sections.append(f"    metrics: dict[str, Any]          # Numeric/boolean measures")
    sections.append(f"    findings: list[str | dict]       # Human-readable observations")
    sections.append(f"    risks: list[str]                 # Identified risks")
    sections.append(f"    samples: dict[str, list]         # Evidence rows (empty {{}} if none)")
    sections.append(f"    suggested_next_actions: list[str] # MUST:/SHOULD: prefixed actions")
    sections.append(f"```")
    sections.append(f"")
    sections.append(f"---")
    sections.append(f"")

    # Per-tool schemas
    errors: list[str] = []
    skipped_tools: list[str] = []
    for name, func, kwargs in registry:
        if skip_slow and name in SLOW_TOOLS:
            skipped_tools.append(name)
            continue

        if kwargs is None:
            sections.append(f"## `{name}`")
            sections.append(f"")
            sections.append(f"_Skipped: required dependency not available._")
            sections.append(f"")
            continue

        try:
            # Handle positional arg for framework_lookup_context
            if name == "framework_lookup_context":
                output = func(kwargs.pop("query"), **kwargs)
            else:
                output = func(**kwargs)
        except Exception as e:
            errors.append(f"{name}: {e}")
            sections.append(f"## `{name}`")
            sections.append(f"")
            sections.append(f"_Error during generation: {e!r}_")
            sections.append(f"")
            continue

        if isinstance(output, str):
            sections.append(f"## `{name}`")
            sections.append(f"")
            sections.append(f"Returns **markdown string** (not dict) when called with default args.")
            sections.append(f"")
            continue

        # Contract compliance
        missing = STANDARD_CONTRACT - set(output.keys())
        extra_keys = sorted(set(output.keys()) - STANDARD_CONTRACT)

        schema = _build_schema(output)
        sig = inspect.signature(func)

        sections.append(f"## `{name}`")
        sections.append(f"")
        sections.append(f"**Signature:** `{name}({sig})`")
        sections.append(f"")

        # Contract status
        if missing:
            sections.append(f"⚠️ Missing contract keys: {sorted(missing)}")
            sections.append(f"")

        # All keys
        sections.append(f"**Output keys:**")
        sections.append(f"")
        sections.append(f"| Key | Type | Contract |")
        sections.append(f"| --- | --- | --- |")
        for key in sorted(output.keys()):
            is_contract = "✓" if key in STANDARD_CONTRACT else ""
            type_str = schema[key]
            sections.append(f"| `{key}` | `{type_str}` | {is_contract} |")
        sections.append(f"")

        # Metrics detail
        metrics = output.get("metrics", {})
        if metrics:
            sections.append(f"**Metrics keys:**")
            sections.append(f"")
            for line in _describe_metrics(metrics):
                sections.append(f"- `{line.strip()}`")
            sections.append(f"")

        # Samples structure
        samples = output.get("samples", {})
        sections.append(f"**Samples:** `{_describe_samples(samples)}`")
        sections.append(f"")
        sections.append(f"---")
        sections.append(f"")

    # Skipped tools section
    if skipped_tools:
        sections.append(f"## Skipped Tools (--fast)")
        sections.append(f"")
        for t in skipped_tools:
            sections.append(f"- `{t}`")
        sections.append(f"")

    # Footer
    if errors:
        sections.append(f"## Generation Errors")
        sections.append(f"")
        for err in errors:
            sections.append(f"- {err}")
        sections.append(f"")

    return "\n".join(sections)


# Tools that are expensive (full AST traversal of the project tree)
SLOW_TOOLS = {
    "codebase_map_context",
    "codebase_map_context_focused",
    "change_impact_context",
    "consistency_check_context",
    "convention_preflight_context",
    "test_focus_context",
    "workflow_gate_context",
    "session_snapshot_context",
    "framework_lookup_context",
    "semantic_edit_context",
    "preflight_context",
    "import_resolve_context",
    "safe_change_context",
}


def main():
    """Generate and write OUTPUT_SCHEMA.md.

    Usage:
        python scripts/generate_schema.py          # Full (~5 min)
        python scripts/generate_schema.py --fast   # Data tools only (~30s)
    """
    import argparse
    parser = argparse.ArgumentParser(description="Generate OUTPUT_SCHEMA.md")
    parser.add_argument("--fast", action="store_true",
                        help="Skip expensive codebase-traversal tools for faster iteration")
    args = parser.parse_args()

    content = generate_schema(skip_slow=args.fast)
    output_path = ROOT / "OUTPUT_SCHEMA.md"
    output_path.write_text(content, encoding="utf-8")
    skipped = " (--fast: skipped slow tools)" if args.fast else ""
    print(f"Generated {output_path} ({len(content):,} chars){skipped}")


if __name__ == "__main__":
    main()
