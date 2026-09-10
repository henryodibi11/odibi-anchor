# test_focus_context — Code Walkthrough

## Purpose

`test_focus_context` maps changed files and functions to the minimal set of test files
that need to be run. It eliminates the "run everything" bottleneck by narrowing the test
surface to only what's affected by a code change. It also reports coverage gaps (functions
with no test references) and generates a ready-to-paste `pytest` run command.

It scopes the dispatcher call `anchor("test", changed_files=[...])` and is also auto-run as
the final step in the `safe_change_context` pipeline. Its fallback command is advisory:
the dispatcher does not silently execute a full suite when no applicable tests are found.

## Public Entrypoints

| Name | Signature | Role |
| --- | --- | --- |
| `test_focus_context` | `(root, *, changed_files, changed_functions, subject, test_dir, src_dir, include_test_ids, output_format)` | Main builder |
| `render_test_focus_report` | `(ctx: dict) -> str` | Renders context dict as markdown |

## Main Execution Flow

```
test_focus_context(root, changed_files=["src/mylib/transform.py"],
                   include_test_ids=True)
|
|-- 1. validate_output_format
|-- 2. Resolve paths: test_root = root/tests, src_root = root/src
|-- 3. _collect_test_files(test_root) -> list of test .py files
|-- 4. _collect_source_files(src_root) -> list of source .py files
|-- 5. _parse_source_definitions(root, source_files)
|       For each source file: AST walk, collect FunctionDef names + module constants
|       -> source_defs: {rel_path: {functions: [...], constants: [...]}}
|-- 6. _parse_test_references(root, test_files)
|       For each test file: collect imports, text refs, test_function names
|       -> test_refs: {rel_path: {imports: [...], test_functions: [...]}}
|-- 7. Resolve all_changed_functions from changed_files:
|       For each changed file: extract module_name + all function/constant names
|       -> all_changed_functions deduplicated list
|-- 8. _find_affected_tests(root, test_refs, all_changed_functions,
|                           changed_modules, changed_files)
|       -> affected: [{path: ..., reason: ...}, ...]
|-- 9. (if include_test_ids) _filter_relevant_tests per affected file
|       -> targeted_test_ids: ["tests/test_transform.py::test_apply_transform", ...]
|       targeted_run_command = "pytest tests/...::test_name --no-header -q"
|-- 10. _find_coverage_gaps(source_defs, test_refs, changed_files, src_dir)
|       -> coverage_gaps: [{function: ..., file: ..., reason: ...}]
|-- 11. _suggest_test_cases(all_changed_functions, source_defs, changed_files)
|-- 12. run_command = "pytest tests/test_X.py tests/test_Y.py -q"
|       (reports "pytest tests/ -q" as an explicit fallback if no affected files are found)
|-- 13. estimated_run_seconds = affected_test_count * 0.3
`-- 14. Build ctx with obligations_created + obligations_paid
```

## Output Contract

```python
{
    "kind": "test_focus_context",
    "subject": str,
    "summary": str,   # "3 changed function(s) → 2 test file(s) affected, 1 coverage gap(s)"
    "metrics": {
        "changed_functions_count": int,
        "affected_test_files_count": int,
        "coverage_gaps_count": int,
        "estimated_run_seconds": float,   # affected_test_count * 0.3
        "test_functions_in_scope": int,
        "targeted_test_ids_count": int,
    },
    # Builder-specific keys:
    "affected_test_files": list[{"path": str, "reason": str}],
    "coverage_gaps": list[{"function": str, "file": str, "reason": str}],
    "run_command": str,                # always present — suggested fallback may be full suite
    "targeted_test_ids": list[str],    # ["path/test.py::test_func", ...]
    "targeted_run_command": str,       # only set when targeted_test_ids non-empty
    "suggested_test_cases": list[str],
    "obligations_created": list[{"tool": str, "reason": str, "priority": str, "suggested_call": str}],
    "obligations_paid": ["test_focus_context"],
    "findings": list[str],
    "risks": list[str],
    "samples": {},
    "suggested_next_actions": list[str],
}
```

This builder is the only one in the codebase module that writes to `obligations_created`
and `obligations_paid`.

## Worked Examples

### Example 1 — Targeted Test Discovery with Test IDs

```
Input:
  changed_files = ["src/mylib/transform.py"]
  include_test_ids = True

Execution:
  _parse_source_definitions for src/mylib/transform.py:
    -> functions = ["apply_transform", "rollback_transform", "_normalize_schema"]
       constants = ["DEFAULT_BATCH_SIZE"]
  all_changed_functions = ["apply_transform", "rollback_transform", "_normalize_schema",
                            "DEFAULT_BATCH_SIZE"]

  _find_affected_tests:
    tests/test_transform.py imports "from src.mylib.transform import apply_transform"
    -> affected = [{path: "tests/test_transform.py", reason: "imports transform"}]
    tests/test_integration.py has text "apply_transform" (not import, direct ref)
    -> affected += [{path: "tests/test_integration.py", reason: "references apply_transform"}]

  _filter_relevant_tests("tests/test_transform.py", [...], ["apply_transform"]):
    test_apply_transform: function body contains "apply_transform" -> included
    test_helper_util: body does not -> excluded
    -> relevant_tests = ["test_apply_transform", "test_rollback_transform"]

  targeted_test_ids = [
    "tests/test_transform.py::test_apply_transform",
    "tests/test_transform.py::test_rollback_transform",
    "tests/test_integration.py",    # whole file (no specific test IDs resolved)
  ]

Output:
  run_command = "pytest tests/test_transform.py tests/test_integration.py -q"
  targeted_run_command = "pytest tests/test_transform.py::test_apply_transform
                               tests/test_transform.py::test_rollback_transform
                               tests/test_integration.py --no-header -q"
  metrics.estimated_run_seconds = 2.4  # 8 test functions * 0.3
  suggested_next_actions[0] = "MUST: Run precise: pytest tests/test_transform.py::..."
```

### Example 2 — Coverage Gap Detection

```
Input:
  changed_files = ["src/mylib/validator.py"]
  include_test_ids = False

Execution:
  source_defs for src/mylib/validator.py:
    functions = ["validate_schema", "validate_row", "_build_error_message"]

  _find_coverage_gaps:
    For "validate_schema": test_refs shows test_validator.py imports and references it
      -> no gap
    For "validate_row": no test file imports or references "validate_row"
      -> coverage_gap: {function: "validate_row", file: "src/mylib/validator.py",
                        reason: "no test references found"}
    For "_build_error_message": private -> skipped

  coverage_gaps = [{...validate_row...}]

Output:
  metrics.coverage_gaps_count = 1
  risks = ["1 function(s) have no test coverage — changes could break silently."]
  suggested_next_actions includes: "MUST: Write tests for 1 uncovered function(s)."
  run_command = "pytest tests/test_validator.py -q"  (if test_validator.py found)
```

## Notable Implementation Patterns

- **Changed-file functions are fully enumerated**: when `changed_files` is provided, ALL
  functions defined in those files are added to `all_changed_functions`. This means test
  discovery is not limited to explicitly specified function names — touching a file
  automatically exposes all its public and private functions for matching.
- **`run_command` always present**: if no affected tests are found, the builder suggests
  `pytest {test_dir}/ -q`. The dispatcher treats that as guidance, not authorization to
  run the full suite; callers must pass an explicit `target` for broader verification.
- **`estimated_run_seconds = affected_test_count * 0.3`**: a fixed 300ms-per-test estimate.
  This is an approximation for test suite time budgeting; actual times vary. The estimate
  is most useful to convey that "targeted run is N seconds vs full suite M seconds."
- **Targeted test IDs cap at 10 in `suggested_next_actions`**: the MUST action truncates
  at `targeted_test_ids[:10]` to keep the command line readable. The full list is available
  in `targeted_test_ids` for programmatic use.
- **`obligations_paid = ["test_focus_context"]`**: this builder self-reports as paid when
  it completes successfully, since running the builder IS the test focus step. The
  obligation to actually run the tests is in `obligations_created`.
- **Coverage gaps skip private functions**: `_build_error_message` (underscore-prefixed)
  is not added to coverage gaps. Only public functions (those not starting with `_`) are
  expected to have test coverage.
