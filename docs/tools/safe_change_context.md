# safe_change_context

`safe_change_context` orchestrates the full edit→verify pipeline: guardrail check,
semantic edit, preflight validation, and test targeting. It answers:

```text
Is this edit safe to apply? What tests should I run after?
```

Combines known_bad_change_context → semantic_edit_context → preflight_context →
test_focus_context in a single call.

## Public API

```python
from odibi_anchor.codebase import safe_change_context

def safe_change_context(
    root: str | Path,
    *,
    target: str,
    action: str,
    function: str | None = None,
    verify: bool = True,
    test: bool = True,
    guardrail: bool = True,
    apply: bool = False,
    subject: str | None = None,
    output_format: str = "dict",
    **edit_kwargs: Any,
) -> dict[str, Any] | str:
    ...
```

## Parameters

| Parameter | Type | Default | Purpose |
|-----------|------|---------|---------|
| `root` | str or Path | required | Project root directory |
| `target` | str | required | File to edit (relative to root) |
| `action` | str | required | Edit action (rename_parameter, add_function, etc.) |
| `function` | str or None | None | Target function |
| `verify` | bool | True | Run preflight checks after edit |
| `test` | bool | True | Run test_focus_context to find affected tests |
| `guardrail` | bool | True | Check known_bad_change_context first |
| `apply` | bool | False | If True, write changes to disk |
| `**edit_kwargs` | Any | | Pass-through to semantic_edit_context |
| `output_format` | str | "dict" | "dict" or "markdown" |

## Output Shape

```python
{
    "kind": "safe_change_context",
    "metrics": {"applied": False, "is_safe": True, "guardrail_status": "ok",
                "preflight_safe": True, "test_count": 5},
    "diff_preview": "...",
    "run_command": "pytest tests/codebase/test_module.py -v",
    "suggested_next_actions": ["MUST: Review diff, then apply with safe_change_context(..., apply=True)"],
}
```

## When To Use

- As the primary entry point for code modifications (replaces manual edit→test cycle)
- When refactoring functions that may have known-bad patterns
- When you want guardrail protection + automatic test targeting
