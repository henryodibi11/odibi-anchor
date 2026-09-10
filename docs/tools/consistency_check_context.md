# consistency_check_context

`consistency_check_context` scans a codebase and checks whether it follows
its own established patterns. Unlike pattern detection (discovery), this tool
**enforces** conventions by reporting violations.

It answers:

```text
Does this codebase follow its own rules?
What's inconsistent?
What needs to be fixed?
```

## Public API

```python
def consistency_check_context(
    root: str | Path,
    *,
    subject: str | None = None,
    rules: list[str] | None = None,
    exclude_rules: list[str] | None = None,
    exclude_paths: list[str] | None = None,
    output_format: str = "dict",
) -> dict[str, Any] | str:
```

```python
def render_consistency_check_report(ctx: dict) -> str:
    """Render violations as markdown report."""
```

## Parameters

| Parameter | Type | Default | Purpose |
| --- | --- | --- | --- |
| `root` | str/Path | required | Project directory to scan |
| `subject` | str | dir name | Human label |
| `rules` | list[str] | all 7 | Which rules to check |
| `exclude_rules` | list[str] | None | Rules to skip |
| `exclude_paths` | list[str] | None | Paths to exclude (e.g., ["tests/", "scripts/"]) |
| `output_format` | str | "dict" | "dict" or "markdown" |

## Built-in Rules

| Rule | What It Checks | Severity |
| --- | --- | --- |
| `export_completeness` | `__all__` matches actual imports in `__init__.py` | warning |
| `render_pairing` | Every `*_context` has a `render_*_report` | warning |
| `output_format_param` | All `*_context` functions accept `output_format` | warning |
| `docstring_coverage` | All public functions have docstrings | info |
| `naming_conventions` | Functions=snake_case, classes=PascalCase | info |
| `test_coverage` | Every source file has matching `test_*` file | warning |
| `return_contract` | `*_context` functions return standard keys (kind, subject, summary, metrics, findings, risks) | blocker |

## Output Shape

```python
{
    "kind": "consistency_check_context",
    "subject": "my_project",
    "summary": "my_project: 3 violation(s) across 2 rule(s). 15 files scanned.",
    "metrics": {
        "rules_checked": 7,
        "violation_count": 3,
        "compliant_count": 28,
        "files_scanned": 15,
        "violation_by_rule": {
            "render_pairing": 2,
            "output_format_param": 1,
        },
    },
    "violations": [
        {
            "rule": "render_pairing",
            "file": "src/tools/my_tool.py",
            "line": None,
            "detail": "my_context has no matching render_my_report",
            "severity": "warning",
        },
    ],
    "compliant": [
        {
            "rule": "export_completeness",
            "file": "src/__init__.py",
            "detail": "__all__ matches imports",
        },
    ],
    "findings": [...],
    "risks": [...],
    "samples": [],
    "suggested_next_actions": ["Fix [render_pairing] in ..."],
}
```

## Example Usage

### Full Check

```python
from odibi_anchor.codebase import consistency_check_context

ctx = consistency_check_context("/path/to/project")

if ctx["metrics"]["violation_count"] > 0:
    print(f"Found {ctx['metrics']['violation_count']} violations:")
    for v in ctx["violations"]:
        print(f"  [{v['rule']}] {v['file']}: {v['detail']}")
```

### Specific Rules Only

```python
ctx = consistency_check_context(
    "/path/to/project",
    rules=["return_contract", "render_pairing"],
    exclude_paths=["scripts/", "examples/"],
)
```

### CI Gate

```python
ctx = consistency_check_context("/path/to/project")
blockers = [v for v in ctx["violations"] if v["severity"] == "blocker"]
if blockers:
    raise RuntimeError(f"Consistency check failed: {len(blockers)} blocker(s)")
```

### Markdown Report

```python
report = consistency_check_context("/path/to/project", output_format="markdown")
print(report)
```

## How It Works

1. **Collect** all `.py` files (excluding hidden dirs, `__pycache__`, excluded paths)
2. **Parse** each file with AST (syntax errors → file skipped)
3. **Extract** structural info: functions, classes, `__all__`, imports
4. **Run** each active rule against the parsed info
5. **Report** violations with file, line, detail, severity

## Design Decisions

- **Stdlib only**: ast, os, pathlib, re. Zero external dependencies.
- **AST-based**: Parses actual Python structure, not string matching.
- **Non-destructive**: Reports only, never modifies code.
- **Severity levels**: blocker (must fix), warning (should fix), info (nice to fix).
- **Handles annotated assignments**: `ctx: dict[str, Any] = {...}` detected correctly.
- **Graceful on errors**: Files with syntax errors are skipped, not crashed.

## Testing

38 tests covering:
- Output contract (kind, subject, metrics)
- Clean project (all rules pass)
- Broken project (each rule detects its violation)
- Rule filtering (single rule, exclude_rules, exclude_paths)
- Output format (dict, markdown, invalid raises)
- Render function (pass/fail reports, grouped by rule)
- Edge cases (empty dir, syntax errors, hidden dirs, pycache)
- Dog-food against odibi_anchor itself
