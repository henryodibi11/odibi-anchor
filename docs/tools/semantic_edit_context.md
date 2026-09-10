# semantic_edit_context

`semantic_edit_context` performs intent-based code editing — you describe *what*
you want to change (action + target), and it generates or applies the edit using
AST-aware transformations. It answers:

```text
How do I safely rename/add/remove this code element?
What does the diff look like before I commit?
```

Uses libcst for precise AST manipulation when available, falls back to regex
when libcst is not installed.

## Public API

```python
from odibi_anchor.codebase import semantic_edit_context

def semantic_edit_context(
    root: str | Path,
    *,
    target: str,
    action: str,
    function: str | None = None,
    class_name: str | None = None,
    param_name: str | None = None,
    param_type: str | None = None,
    param_default: str | None = None,
    param_keyword_only: bool = True,
    new_name: str | None = None,
    decorator: str | None = None,
    return_type: str | None = None,
    import_statement: str | None = None,
    apply: bool = False,
    subject: str | None = None,
    output_format: str = "dict",
) -> dict[str, Any] | str:
    ...
```

## Parameters

| Parameter | Type | Default | Purpose |
|-----------|------|---------|---------|
| `root` | str or Path | required | Project root directory |
| `target` | str | required | Relative path to file being edited |
| `action` | str | required | Edit action (rename_parameter, add_parameter, etc.) |
| `function` | str or None | None | Target function name |
| `class_name` | str or None | None | Target class name |
| `param_name` | str or None | None | Parameter to modify |
| `new_name` | str or None | None | New name for renames |
| `apply` | bool | False | If True, write changes to disk |
| `output_format` | str | "dict" | "dict" or "markdown" |

## Output Shape

```python
{
    "kind": "semantic_edit_context",
    "metrics": {"action": "rename_parameter", "applied": False, "additions": 2, "deletions": 2},
    "diff_preview": "--- a/src/module.py\n+++ b/src/module.py\n...",
    "suggested_next_actions": ["MUST: Review diff_preview before applying.", ...],
}
```

## When To Use

- When you need to rename a function/parameter/class safely
- When adding parameters with correct placement
- When you want a preview before applying changes
