# preflight_context

`preflight_context` runs lint (ruff), type checking (pyright), and syntax
validation on changed files, then returns structured diagnostics. It answers:

```text
Does this code have type errors or lint violations?
Is it safe to proceed after my edit?
```

Degrades gracefully if ruff or pyright are not installed (skips those checks).

## Public API

```python
from odibi_anchor.codebase import preflight_context

def preflight_context(
    root: str | Path,
    *,
    changed_files: list[str] | None = None,
    check_types: bool = True,
    check_lint: bool = True,
    check_syntax: bool = True,
    subject: str | None = None,
    output_format: str = "dict",
) -> dict[str, Any] | str:
    ...
```

## Parameters

| Parameter | Type | Default | Purpose |
|-----------|------|---------|---------|
| `root` | str or Path | required | Project root directory |
| `changed_files` | list[str] or None | None | Files to check (all .py if None) |
| `check_types` | bool | True | Run pyright type checking |
| `check_lint` | bool | True | Run ruff linting |
| `check_syntax` | bool | True | Validate Python syntax |
| `subject` | str or None | None | Human label |
| `output_format` | str | "dict" | "dict" or "markdown" |

## Output Shape

```python
{
    "kind": "preflight_context",
    "metrics": {"error_count": 0, "warning_count": 1, "is_safe": True},
    "findings": ["SAFE: 0 error(s), 1 warning(s)"],
    "suggested_next_actions": ["MUST: Run test_focus_context(...)"],
}
```

## When To Use

- After any code edit (especially with apply=True) to verify no type errors
- Before committing changes to catch lint issues early
- As part of the safe_change_context pipeline
