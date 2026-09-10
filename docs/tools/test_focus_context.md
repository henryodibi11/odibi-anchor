# test_focus_context

**Package:** `odibi_anchor.codebase`  
**Purpose:** Map changed functions/files → affected test files. Report coverage gaps. Suggest test cases.

## When to Use

- After editing source files — find which tests to run (avoid full-suite penalty)
- When adding new functions — identify coverage gaps immediately
- During code review — verify test coverage for changed code

## Usage

```python
from odibi_anchor.codebase import test_focus_context

# After editing a source file
ctx = test_focus_context(
    "/path/to/project",
    changed_files=["src/mylib/transform.py"],
)
print(ctx["run_command"])
# "pytest tests/test_transform.py -q"

# Or search by function name
ctx = test_focus_context(
    "/path/to/project",
    changed_functions=["process_data", "validate_input"],
)
```

## Parameters

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `root` | `str \| Path` | required | Project root directory |
| `changed_files` | `list[str] \| None` | `None` | Changed file paths (relative to root) |
| `changed_functions` | `list[str] \| None` | `None` | Changed function names |
| `subject` | `str \| None` | dirname | Human label |
| `test_dir` | `str` | `"tests"` | Test directory name |
| `src_dir` | `str` | `"src"` | Source directory name |
| `output_format` | `str` | `"dict"` | `"dict"` or `"markdown"` |

## Output Contract

```python
{
    "kind": "test_focus_context",
    "subject": "my_project",
    "summary": "5 changed function(s) → 2 test file(s) affected, 1 coverage gap(s)",
    "metrics": {
        "changed_functions_count": 5,
        "affected_test_files_count": 2,
        "coverage_gaps_count": 1,
        "estimated_run_seconds": 12.3,
        "test_functions_in_scope": 41,
    },
    "affected_test_files": [
        {"path": "tests/test_transform.py", "reason": "imports transform; references process_data"},
    ],
    "coverage_gaps": [
        {"function": "validate_input", "file": "src/mylib/utils.py", "reason": "no test references this function"},
    ],
    "run_command": "pytest tests/test_transform.py -q",
    "suggested_test_cases": ["test_validate_input_returns_expected_output", ...],
    "findings": [...],
    "risks": [...],
    "samples": [],
    "suggested_next_actions": [...],
}
```

## How It Works

1. **AST-parses source files** to extract function and constant definitions
2. **AST-parses test files** to extract imports, function references, and string literals
3. **Matches** changed functions/modules against test file references
4. **Name-convention matching** — `test_foo.py` matches `foo.py`
5. **Coverage gaps** = public functions/constants with no test file references

## Render Function

```python
from odibi_anchor.codebase import render_test_focus_report

md = render_test_focus_report(ctx)  # or output_format="markdown"
```

## Dog-food Result (odibi_anchor)

```
Changed: src/odibi_anchor/codebase/consistency_check_context.py
→ 1 test file affected (tests/test_consistency_check_context.py)
→ 0 coverage gaps
→ Run command: pytest tests/test_consistency_check_context.py -q
→ Estimated: 11.4s | Actual: 13.4s | Full suite: ~290s
→ Speedup: 22x
```
