# import_resolve_context

`import_resolve_context` resolves the correct import path for a symbol by
scanning the project source tree. It answers:

```text
What is the correct import statement for this class/function?
Is it exported publicly or only available internally?
```

## Public API

```python
from odibi_anchor.codebase import import_resolve_context

def import_resolve_context(
    root: str | Path,
    *,
    symbol: str,
    target_file: str | None = None,
    prefer_public: bool = True,
    subject: str | None = None,
    output_format: str = "dict",
) -> dict[str, Any] | str:
    ...
```

## Parameters

| Parameter | Type | Default | Purpose |
|-----------|------|---------|---------|
| `root` | str or Path | required | Project root directory |
| `symbol` | str | required | Symbol name to resolve (class, function, constant) |
| `target_file` | str or None | None | File where the import will be used |
| `prefer_public` | bool | True | Prefer public API paths over internal ones |
| `subject` | str or None | None | Human label |
| `output_format` | str | "dict" | "dict" or "markdown" |

## Output Shape

```python
{
    "kind": "import_resolve_context",
    "metrics": {"candidates_found": 2, "is_public": True},
    "import_statement": "from odibi_anchor.codebase import memory_context",
    "candidates": [
        {"path": "odibi_anchor.codebase", "is_public": True},
        {"path": "odibi_anchor.codebase.memory_context", "is_public": False},
    ],
    "suggested_next_actions": [...],
}
```

## When To Use

- When you can't remember the correct import path for a symbol
- When resolving between public API and internal module paths
- Before adding an import statement to verify it exists
