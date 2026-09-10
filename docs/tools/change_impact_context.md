# change_impact_context

`change_impact_context` analyzes the ripple effect of changing a file or
function. It maps all importers, call sites, test files, and documentation
references, then produces a risk assessment and actionable update checklist.

It answers:

```text
What depends on this code?
Is this change breaking or backward-compatible?
What files need updating?
Will existing tests catch breakage?
```

Designed to eliminate the "grep → read → grep" cycle — the 3-5 tool calls
an AI agent performs to understand what a change will affect.

## Public API

```python
from odibi_anchor.codebase import change_impact_context

def change_impact_context(
    root: str | Path,
    *,
    target: str,
    function: str | None = None,
    change_type: str = "modify_logic",
    subject: str | None = None,
    include_docs: bool = True,
    doc_dirs: list[str] | None = None,
    output_format: str = "dict",
) -> dict[str, Any] | str:
    ...
```

## Parameters

| Parameter | Type | Default | Purpose |
|-----------|------|---------|---------|
| `root` | str or Path | required | Project root directory |
| `target` | str | required | Relative path to the file being changed |
| `function` | str or None | None | Specific function to analyze (narrows search) |
| `change_type` | str | `"modify_logic"` | Type of change (determines risk level) |
| `subject` | str or None | None | Human label. Defaults to function or file stem |
| `include_docs` | bool | True | Scan doc directories for references |
| `doc_dirs` | list[str] | `["docs", "skills"]` | Directories to scan for documentation |
| `output_format` | str | `"dict"` | `"dict"` or `"markdown"` |

## Change Types & Risk Levels

| Change Type | Risk | Breaking? | Description |
|-------------|------|-----------|-------------|
| `add_parameter` | low | No | New kwarg with default — backward-compatible |
| `add_function` | low | No | No existing code affected |
| `modify_logic` | low | No | Internal change, interface stable |
| `refactor` | medium | No* | May change behavior |
| `change_return_type` | medium | No* | May break callers depending on usage |
| `change_return_shape` | medium | No* | Dict key changes may break consumers |
| `add_required_parameter` | high | Yes | All callers must update |
| `remove_parameter` | high | Yes | Callers using it will fail |
| `rename_parameter` | high | Yes | Keyword callers will fail |
| `rename_function` | high | Yes | All importers must update |
| `delete_function` | high | Yes | All callers break |

*Medium becomes breaking if call_site_count > 5.

## Output Shape

```python
{
    "kind": "change_impact_context",
    "subject": "error_trace_context",
    "summary": "error_trace_context: 1 importer(s), 3 test file(s), 3 doc(s). LOW risk (backward-compatible).",
    "metrics": {
        "target": "src/my_pkg/debugging/error_trace_context.py",
        "function": "error_trace_context",
        "change_type": "add_parameter",
        "importer_count": 1,
        "call_site_count": 23,
        "test_file_count": 3,
        "doc_reference_count": 3,
        "risk_level": "low",
        "is_breaking": False,
    },
    "risk_assessment": {
        "level": "low",
        "is_breaking": False,
        "confidence": "medium",
        "note": "Change is backward-compatible but verify behavior.",
        "change_type": "add_parameter",
    },
    "importers": [
        {"file": "debugging/__init__.py", "line": 3, "imported_names": [...], "usage": "re-export"},
    ],
    "call_sites": [
        {"file": "tests/test_render.py", "line": 38, "code": "error_trace_context(exc, df=df)"},
    ],
    "test_files": [
        {"file": "tests/test_render.py", "matched_terms": [...], "test_count": 41},
    ],
    "doc_references": [
        {"file": "docs/tools/error_trace_context.md", "matched_terms": [...], "sections": [...]},
    ],
    "checklist": [
        "Update documentation: docs/tools/error_trace_context.md",
        "Update/add tests in 3 test file(s)",
    ],
    "findings": [...],
    "risks": [...],
    "samples": [],
    "suggested_next_actions": [...],
}
```

## Example Usage

### Before Adding a Parameter

```python
from odibi_anchor.codebase import change_impact_context

ctx = change_impact_context(
    root="/path/to/project",
    target="src/my_pkg/validation/quality_gate_context.py",
    function="quality_gate_context",
    change_type="add_parameter",
)

print(ctx["summary"])
# "quality_gate_context: 3 importers, 4 test files, 9 docs. LOW risk (backward-compatible)."
print(ctx["checklist"])
# ["Update documentation: ...", "Update/add tests in 4 file(s)"]
```

### Before Renaming a Function

```python
ctx = change_impact_context(
    root="/path/to/project",
    target="src/my_pkg/validation/quality_gate_context.py",
    function="quality_gate_context",
    change_type="rename_function",
)

# HIGH risk — 49-item checklist telling you exactly what to update
for item in ctx["checklist"]:
    print(f"  [ ] {item}")
```

### File-Level Analysis (no specific function)

```python
ctx = change_impact_context(
    root="/path/to/project",
    target="src/my_pkg/_utils/engine_utils.py",
    change_type="refactor",
)
# Shows all files that import anything from engine_utils
```

### Markdown for LLM Decision

```python
report = change_impact_context(
    root="/path/to/project",
    target="src/my_pkg/tables/diff_ops.py",
    function="diff_tables_by_key",
    change_type="change_return_shape",
    output_format="markdown",
)
```

## Performance Notes

- **Zero external dependencies** — stdlib only (ast, os, pathlib, re).
- **Fast** — Scans ~40 files in < 2 seconds for function-level analysis.
- **No execution** — Pure static analysis. Never imports or runs code.
- **Regex + AST** — Uses AST for import analysis, regex for call site detection.

## When To Use

Use this:

- **before any change** to understand the blast radius;
- before renaming functions/parameters (get the full update list);
- before deleting code (find all dependents);
- to assess whether a change is breaking or backward-compatible;
- to generate an update checklist for systematic refactoring.

## Gotchas

- `target` must be a relative path from `root` (not absolute).
- Call site detection uses regex (`function_name(`) — may have false positives
  in comments or strings.
- Import analysis is AST-based (precise) but only finds direct imports.
  Re-exports through `__init__.py` are detected as "re-export" usage.
- `doc_dirs` defaults to `["docs", "skills"]`. Customize if your docs live elsewhere.
- `output_format="invalid"` raises `ValueError`.
- `function=None` (file-level) finds all importers of the module, not a specific function.
