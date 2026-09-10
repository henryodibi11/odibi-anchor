# preflight_context — Code Walkthrough

## Purpose

`preflight_context` runs pre-commit checks on changed Python files and returns structured
diagnostics. It provides fast (~2 s) safety verification — syntax, lint, and optionally
types — before the agent proceeds to run a full test suite or call `anchor("gate")`.

It is invoked by the dispatcher as `anchor("preflight", changed_files=[...])` and is also
auto-chained inside `anchor("touched")` to surface syntax failures immediately after any
file edit.

## Public Entrypoints

| Name | Signature | Role |
| --- | --- | --- |
| `preflight_context` | `(root, *, changed_files, check_types, check_lint, check_syntax, subject, output_format, baseline)` | Main builder — runs all checks and returns context dict or markdown |
| `render_preflight_report` | `(ctx: dict) -> str` | Renders a preflight context dict as a markdown report |

## Main Execution Flow

```
preflight_context(root, changed_files=["lib/extractor.py"])
|
|-- 1. validate_output_format(output_format)
|-- 2. Resolve root to absolute Path; filter changed_files to .py files only
|-- 3. ast_cache_invalidate(fp)  <- called for every resolved file path
|
|-- 4. check_syntax=True  -> _check_syntax(file_paths)
|       `-- for each .py file: ast.parse(source)
|          on SyntaxError: append diagnostic {source: "syntax", rule: "SyntaxError", ...}
|
|-- 5. check_types=False (default, disabled: too many false positives on workspace files)
|       if True: shutil.which("pyright") -> _run_pyright(root, file_paths)
|
|-- 6. check_lint=True (default)
|       shutil.which("ruff") -> _run_ruff(root, file_paths)
|       ruff check --output-format json -> parse JSON list -> append diagnostics
|
|-- 7. Manifest required_gates check
|       load_manifest(root) -> constraints.required_gates
|       compare against _SESSION_TIMINGS action set
|       -> _missing_gates list
|
|-- 8. Compute metrics
|       errors = [d for d in diagnostics if d["severity"] == "error"]
|       warnings = [d for d in diagnostics if d["severity"] == "warning"]
|       is_safe = len(errors) == 0
|
|-- 9. Baseline filtering (when baseline set passed)
|       _error_fingerprint(diag) = "source:rule:basename"
|       new_errors = errors NOT in baseline fingerprints
|       baseline_errors = errors IN baseline fingerprints
|       is_safe based on new_errors only
|       first run (no baseline): capture current_baseline via _captured_baseline metric
|
|-- 10. Build findings, risks, suggested_next_actions
|
`-- 11. build_base_context(...) -> ctx dict
         if output_format == "markdown": render_preflight_report(ctx)
```

## Helper Responsibilities

| Helper | Responsibility |
| --- | --- |
| `_check_syntax(files)` | Calls `ast.parse()` on each .py file; catches `SyntaxError`; zero dependencies |
| `_run_pyright(root, files)` | `subprocess.run(["pyright", "--outputjson"])`, parses `generalDiagnostics` list |
| `_run_ruff(root, files)` | `subprocess.run(["ruff", "check", "--output-format", "json"])`, parses JSON list; ruff diagnostics with a `fix` key get `severity: "warning"`, others `"error"` |
| `_find_py_files(root)` | `root.rglob("*.py")` excluding `.git`, `__pycache__`, `.venv`, `venv`, `node_modules`, `.tox` |
| `_error_fingerprint(diag)` | Returns `"source:rule:basename"` — excludes message to survive pyright's non-deterministic union type ordering |

## Output Contract

```python
{
    "kind": "preflight_context",
    "subject": str,                    # directory name or user label
    "summary": str,                    # "SAFE: 0 error(s), 2 warning(s) across 1 file(s)"
    "metrics": {
        "files_checked": int | "all",
        "total_diagnostics": int,
        "errors": int,
        "warnings": int,
        "type_errors": int,            # pyright-sourced errors
        "lint_errors": int,            # ruff-sourced errors
        "syntax_errors": int,          # ast-sourced errors
        "is_safe": bool,
        "tools_available": dict,       # {"ruff": bool, "pyright": bool}
        "missing_required_gates": list[str],
        # Baseline mode only:
        "baseline_errors": int,
        "new_errors": int,
        # First-run capture:
        "_captured_baseline": list[str],  # sorted fingerprints for JSON serialization
    },
    "findings": list[str],
    "risks": list[str],
    "samples": {},
    "suggested_next_actions": list[str],
    "diagnostics": list[dict],         # raw diagnostic dicts -- builder-specific key
}
```

The `diagnostics` list is the builder-specific field not present on other context shapes.
Each diagnostic dict has keys: `file`, `line`, `column`, `message`, `severity`, `source`,
`rule`.

## Worked Examples

### Example 1 — Syntax Error Caught

```
Input:
  root = "/project"
  changed_files = ["lib/extractor.py"]
  extractor.py contains:  def foo(   # unclosed paren

Execution:
  Step 1: file_paths = [Path("/project/lib/extractor.py")]
  Step 2: ast_cache_invalidate(Path("/project/lib/extractor.py"))
  Step 3: check_syntax=True -> _check_syntax([Path("/project/lib/extractor.py")])
            ast.parse(source) raises SyntaxError("unexpected EOF while parsing")
            diagnostic appended:
              {"file": "/project/lib/extractor.py", "line": 1, "column": 10,
               "message": "unexpected EOF while parsing",
               "severity": "error", "source": "syntax", "rule": "SyntaxError"}
  Step 4: errors = [<above diagnostic>], is_safe = False
  Step 5: summary = "UNSAFE: 1 error(s), 0 warning(s) across 1 file(s)"

Output ctx (key fields):
  metrics.is_safe = False
  metrics.syntax_errors = 1
  diagnostics = [{...syntax error...}]
  findings = ["1 error(s) found -- fix before proceeding.",
              "  /project/lib/extractor.py:1 -- unexpected EOF while parsing"]
  risks = ["1 error(s) -- changes will likely break at runtime."]
```

`anchor("touched")` auto-runs this check and surfaces `syntax_check: "FAILED"` in its
result dict so the agent sees the failure immediately without waiting for gate.

### Example 2 — Baseline Filtering with Ruff Warnings

```
Input:
  root = "/project"
  changed_files = ["lib/renderer.py"]
  baseline = {"ruff:E501:renderer.py", "ruff:W291:renderer.py"}  # 2 existing warnings
  ruff finds 3 warnings in renderer.py:
    {source:"ruff", rule:"E501", file:"renderer.py", ...}
    {source:"ruff", rule:"W291", file:"renderer.py", ...}
    {source:"ruff", rule:"F401", file:"renderer.py", ...}  <- new

Execution:
  Step 1: _run_ruff returns all 3 as severity="warning" (all have fix keys)
  Step 2: baseline filtering:
            fingerprints = ["ruff:E501:renderer.py", "ruff:W291:renderer.py",
                            "ruff:F401:renderer.py"]
            baseline_errors = first 2 (match baseline)
            new_errors = ["ruff:F401:renderer.py"]  <- 1 new
            is_safe = len(new_errors) == 0 -> False
  Step 3: findings_baseline_note = "2 pre-existing error(s) in baseline (not blocking)."

Output ctx (key fields):
  metrics.baseline_errors = 2
  metrics.new_errors = 1
  metrics.is_safe = False
  findings = ["2 pre-existing error(s) in baseline (not blocking).",
              "1 error(s) found -- fix before proceeding.", ...]
```

Without a baseline, `_captured_baseline` is populated on first run so the caller can
pass it back on subsequent calls to suppress pre-existing noise.

## Notable Implementation Patterns

- **Tool availability via `shutil.which()`**: both pyright and ruff are looked up at call
  time; if not installed, the check is silently skipped. Only syntax checking (stdlib
  `ast.parse`) is always available.
- **`check_types=False` by default**: pyright produces too many false positives on
  Databricks workspace files; type checking must be opted-in explicitly.
- **`_error_fingerprint` excludes message**: pyright can reorder union type members
  between runs (`str | dict` vs `dict | str`), so message-based fingerprints would
  misidentify identical errors as new. Using `source:rule:basename` is stable.
- **AST cache invalidation**: `ast_cache_invalidate(fp)` is called for every resolved
  path before checks run, ensuring downstream `anchor("map")` and `anchor("impact")` see
  up-to-date AST data after edits.
- **Manifest gate check**: if `.anchor_manifest.json` declares `required_gates`, the builder
  inspects `_SESSION_TIMINGS` to find un-run gates and surfaces them in findings and risks.
