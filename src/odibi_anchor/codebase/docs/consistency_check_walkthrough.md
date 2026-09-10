# consistency_check_context — Code Walkthrough

## Purpose

`consistency_check_context` enforces internal project conventions by scanning the codebase
and reporting violations. Unlike a discovery tool, it checks whether the code follows its own
established patterns: render-function pairing, `output_format` parameter consistency,
docstring coverage, naming conventions, export completeness, test coverage, and return
contract compliance.

It is invoked by the dispatcher as `anchor("consistency")` and is most useful after adding new
functions or reorganising modules to verify nothing drifted from project conventions.

## Public Entrypoints

| Name | Signature | Role |
| --- | --- | --- |
| `consistency_check_context` | `(root, *, subject, rules, exclude_rules, exclude_paths, custom_rules, changed_files, scope, suppress_suggestions, output_format)` | Main builder |
| `render_consistency_check_report` | `(ctx: dict) -> str` | Renders context dict as markdown |

## Built-In Rules

| Rule | What it checks |
| --- | --- |
| `export_completeness` | `__all__` in `__init__.py` matches actual imports/definitions |
| `render_pairing` | Every `*_context` function has a matching `render_*_report` |
| `output_format_param` | Public functions that accept output take `output_format` kwarg |
| `docstring_coverage` | Public (non-private) functions have docstrings |
| `naming_conventions` | snake_case for functions/vars; UPPER_CASE for module-level constants |
| `test_coverage` | Every source module has a corresponding test file |
| `return_contract` | Public functions return standard dict keys (kind, subject, summary, metrics) |

## Main Execution Flow

```
consistency_check_context(root)
|
|-- 1. validate_output_format(output_format)
|-- 2. Resolve root; default active_rules to all 7 built-in rules
|       rules param: explicit list; exclude_rules param: subtracted from active set
|-- 3. _collect_python_files(root, exclude_paths)
|-- 4. Parse each file: _parse_file_info(fpath)
|       For each file: ast.iter_child_nodes
|         FunctionDef/AsyncFunctionDef -> _extract_function_info (name, line, params,
|             has_output_format, is_private, docstring, decorators)
|         ClassDef -> class_info (name, line, methods, docstring)
|         Assign: detect __all__ = [...] -> info["all_exports"]
|         Import/ImportFrom -> info["imports"]
|       info["is_init"] = fpath.name == "__init__.py"
|       info["is_test"] = startswith("test_") or "/tests/" in path
|-- 5. For each active_rule: run_rule_runners[rule](root, file_info)
|       Returns (violations, compliant) tuples
|       violations.extend(rule_violations)
|       compliant.extend(rule_compliant)
|-- 6. Run custom_rules (if any)
|       _validate_custom_rule: requires {name, pattern, message}
|       _run_custom_rule: regex.compile(pattern), scan files via fnmatch + line iteration
|-- 7. Scope filtering
|       if scope == "changed" and changed_files:
|         violations = [v for v in violations if v["file"] ends/contains changed file]
|         (full scan still runs; only output is filtered)
|-- 8. Build metrics, summary, findings, risks, suggested actions
|-- 9. _build_post_change_suggestions (if changed_files and not suppress_suggestions)
`-- 10. Return ctx dict or render_consistency_check_report(ctx)
```

## Helper Responsibilities

| Helper | Responsibility |
| --- | --- |
| `_collect_python_files` | `walk_py_files(root)` with exclude_paths prefix filter |
| `_parse_file_info` | AST parse -> extracts functions, classes, __all__, imports, is_init, is_test |
| `_extract_function_info` | Extracts name, line, params (positional + kwonly), has_output_format, is_private, docstring, decorators |
| `_check_export_completeness` | Compares __all__ set to imported_names set; orphaned = in __all__ but not imported; missing = imported but not in __all__ |
| `_check_render_pairing` | Collects *_context names and render_*_report names; flags unpaired context functions |
| `_check_output_format_param` | Flags public functions whose name ends with `_context` but lacks `output_format` param |
| `_check_docstring_coverage` | Flags public (non-private) functions without docstrings |
| `_check_naming_conventions` | Regex checks: `^[a-z_][a-z0-9_]*$` for functions/vars; `^[A-Z_][A-Z0-9_]*$` for module-level constants |
| `_check_test_coverage` | For each non-test .py file, checks if a `tests/test_{stem}.py` exists |
| `_check_return_contract` | Flags `*_context` functions whose return dicts lack standard keys |
| `_run_custom_rule` | `re.compile(pattern)` + line-by-line scan; `fnmatch` for file_filter |
| `_validate_custom_rule` | Requires `{name, pattern, message}`; checks pattern is a string |

## Output Contract

```python
{
    "kind": "consistency_check_context",
    "subject": str,
    "summary": str,   # "project: 3 violation(s) across 2 rule(s). 48 files scanned."
    "metrics": {
        "rules_checked": int,
        "violation_count": int,
        "compliant_count": int,
        "files_scanned": int,
        "violation_by_rule": dict[str, int],   # {"render_pairing": 1, ...}
        "scoped_to_changed": bool,
        "total_violations_found": int,
        "violations_in_scope": int,
    },
    # Builder-specific keys:
    "violations": list[{
        "rule": str,
        "file": str,
        "line": int | None,
        "detail": str,
        "severity": str,  # "blocker" | "warning" | "info"
    }],
    "compliant": list[{"rule": str, "file": str, "detail": str}],
    "post_change_suggestions": list[str],
    "findings": list[str],
    "risks": list[str],
    "samples": {},
    "suggested_next_actions": list[str],
}
```

`violations` and `compliant` are the builder-specific keys. `post_change_suggestions`
appears when `changed_files` is provided and `suppress_suggestions=False`.

## Worked Examples

### Example 1 — Render Pairing Violation

```
Scenario:
  A new "aggregate_context" function was added to lib/aggregator.py
  but its matching "render_aggregate_report" function was forgotten.

Execution:
  _check_render_pairing scans all non-test, non-init, non-_utils files:
    context_funcs = {"preflight_context": "codebase/preflight_context.py",
                     ...,
                     "aggregate_context": "lib/aggregator.py"}
    render_funcs = {"render_preflight_report", ..., "render_change_impact_report"}
    # "render_aggregate_report" is NOT in render_funcs
  For "aggregate_context": expected render = "render_aggregate_report"
  "render_aggregate_report" not found -> violation

Output (violations excerpt):
  {
    "rule": "render_pairing",
    "file": "lib/aggregator.py",
    "line": None,
    "detail": "aggregate_context has no matching render_aggregate_report",
    "severity": "warning",
  }
  summary = "project: 1 violation(s) across 1 rule(s). 52 files scanned."
```

### Example 2 — Custom Rule for Framework Anti-Pattern

```
Scenario:
  Team wants to block all direct spark.read calls project-wide.

  ctx = consistency_check_context(
      root="/project",
      custom_rules=[{
          "name": "no_spark_read",
          "pattern": r"spark[.]read[.]",
          "message": "Use the project's approved data-access path",
          "severity": "blocker",
          "file_filter": "*.py",
      }]
  )

Execution:
  _validate_custom_rule: name, pattern, message all present; pattern is str -> OK
  _run_custom_rule:
    For each .py file: fnmatch("*.py") passes; re.compile(r"spark[.]read[.]")
    pipeline/loader.py line 15: "df = spark.read.csv(path)" -> match
  -> violations = [{
       "rule": "no_spark_read",
       "file": "pipeline/loader.py",
       "line": 15,
       "detail": "Use the project's approved data-access path",
       "severity": "blocker",
     }]
```

## Notable Implementation Patterns

- **`scope="changed"` does NOT short-circuit full scan**: the builder always scans
  everything; the changed scope filter is applied only at output time. This ensures
  cross-reference rules (like `export_completeness`) have full context even when
  only checking changed-file violations.
- **`_check_export_completeness` produces two violation severities**: names in `__all__`
  but not imported get `severity: "warning"`; names imported but missing from `__all__` get
  `severity: "info"`. This distinguishes stale exports (more serious) from unlisted names.
- **`_check_render_pairing` skips `_utils/` and test/init files**: utility modules provide
  helpers, not context tools, so they are excluded from the pairing check entirely.
- **Custom rules use `fnmatch` for file filtering**: the `file_filter` field is matched
  against `fpath.name` (not the full path), so `"*.py"` matches any Python file regardless
  of directory depth.
- **`post_change_suggestions`**: when `changed_files` is provided, the builder derives
  additional suggestions based on which modules changed (e.g., if a test file changed,
  suggest running `anchor("test")` focused on that file).
