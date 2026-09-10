# semantic_edit_context — Code Walkthrough

## Purpose

`semantic_edit_context` is the intent-based code editor. Instead of text replacement, the
caller describes *what* to change in semantic terms (add parameter, rename function, etc.)
and the builder produces a correct diff. It uses `libcst` when available for
formatting-preserving transforms, falling back to a stdlib `ast`-based approach.

It is invoked directly as `anchor("semantic", ...)` or via the `safe_change_context` pipeline.
`apply=False` (dry-run) is the default — callers review the diff before writing to disk.

## Public Entrypoints

| Name | Signature | Role |
| --- | --- | --- |
| `semantic_edit_context` | `(root, *, target, action, function, class_name, param_name, param_type, param_default, param_keyword_only, new_name, decorator, return_type, import_statement, params, body, apply, subject, output_format)` | Main builder |
| `render_semantic_edit_report` | `(ctx: dict) -> str` | Renders context dict as markdown |

## Supported Actions

| action | Required kwargs | Notes |
| --- | --- | --- |
| `add_function` | `params`, `body` | Appends new function to file |
| `add_parameter` | `function`, `param_name` | `param_keyword_only=True` by default |
| `remove_parameter` | `function`, `param_name` | |
| `rename_parameter` | `function`, `param_name`, `new_name` | |
| `rename_function` | `function`, `new_name` | Updates def line only |
| `replace_function` | `function`, `body` | Replaces entire function body |
| `add_decorator` | `function`, `decorator` | Leading `@` is stripped automatically |
| `remove_decorator` | `function`, `decorator` | |
| `change_return_type` | `function`, `return_type` | Annotation only |
| `add_import` | `import_statement` | Appends to import section |
| `remove_import` | `import_statement` | Removes matching import line |

Actions that require function to already exist (`_FUNCTION_REQUIRED_ACTIONS`):
`add_parameter`, `remove_parameter`, `rename_function`, `rename_parameter`,
`add_decorator`, `remove_decorator`, `change_return_type`, `replace_function`.

## Transform Engine Selection

```
libcst installed?
  Yes -> _apply_libcst_transform()   # Formatting-preserving; preferred
  No  -> _apply_ast_transform()      # Stdlib ast; may not preserve comments/formatting
```

`metrics.engine` reports which engine was used. If `ast` is used, a risk entry is added
to `risks` recommending `pip install libcst`.

## Main Execution Flow

```
semantic_edit_context(root, target="lib/reader.py",
                      action="add_parameter",
                      function="read_excel",
                      param_name="strict_mode",
                      param_type="bool",
                      param_default="False")
|
|-- 1. validate_output_format
|-- 2. Validate action in _SUPPORTED_ACTIONS
|-- 3. Validate target_path exists within root
|-- 4. Normalise decorator (strip leading @)
|-- 5. Read original_source = target_path.read_text()
|-- 6. Choose engine (libcst vs ast)
|-- 7. Try: _apply_{engine}_transform(original_source, action=action, ...)
|       on exception: modified_source = original_source; transform_error = str(exc)
|-- 8. difflib.unified_diff(original_source.splitlines(), modified_source.splitlines())
|       -> diff_preview string
|       additions = count lines starting "+" (not "+++")
|       deletions = count lines starting "-" (not "---")
|       has_changes = additions > 0 or deletions > 0
|-- 9. If apply=True and has_changes and not transform_error:
|       target_path.write_text(modified_source)
|       ast_cache_invalidate(target_path)        # evict stale cached AST
|       _write_patch_record(root, target, ...)   # append to .patch_log.jsonl
|         patch_id = "p-{12-char UUID hex}"
|         before_hash = sha256(original_source)[:16]
|         after_hash  = sha256(modified_source)[:16]
|-- 10. function_not_found detection:
|       if function set AND action in _FUNCTION_REQUIRED_ACTIONS
|          AND not has_changes AND not transform_error:
|         function_not_found = not _function_exists_in_source(original_source, function)
`-- 11. Build output ctx
```

## Output Contract

```python
{
    "kind": "semantic_edit_context",
    "subject": str,
    "summary": str,   # "add_parameter on read_excel: 2 addition(s), 0 deletion(s) (dry-run)"
    "metrics": {
        "engine": str,              # "libcst" | "ast"
        "action": str,
        "target": str,
        "function": str | None,
        "lines_added": int,
        "lines_removed": int,
        "has_changes": bool,
        "applied": bool,
        "has_error": bool,
        "function_not_found": bool,  # builder-specific
        "patch_id": str | None,
    },
    # Builder-specific keys:
    "diff_preview": str,
    "modified_source": str,      # full modified source if has_changes, else ""
    "transform_error": str | None,
    "patch_id": str | None,
    "findings": list[str],
    "risks": list[str],
    "samples": {},
    "suggested_next_actions": list[str],
}
```

`diff_preview` and `modified_source` are absent from all other context builder outputs.

## Worked Examples

### Example 1 — Add Keyword-Only Parameter (Dry Run)

```
Input:
  target = "lib/reader.py"
  action = "add_parameter"
  function = "read_excel"
  param_name = "strict_mode"
  param_type = "bool"
  param_default = "False"
  param_keyword_only = True   # (default)

Execution:
  engine = "libcst"  (installed)
  _apply_libcst_transform:
    Finds FunctionDef named "read_excel"
    Appends keyword-only param "strict_mode: bool = False" after existing *-separator
    Returns modified_source with new parameter

  difflib.unified_diff:
    "--- a/lib/reader.py"
    "+++ b/lib/reader.py"
    "@@ -14,6 +14,7 @@"
    " def read_excel("
    "     path: str,"
    "+    strict_mode: bool = False,"
    " ) -> pd.DataFrame:"
  additions=1, deletions=0, has_changes=True
  apply=False -> no write, no patch log

Output:
  metrics.has_changes = True
  metrics.applied = False
  metrics.engine = "libcst"
  diff_preview = "--- a/lib/reader.py
+++ b/lib/reader.py
@@ ..."
  suggested_next_actions = ["MUST: Review diff_preview before applying.",
                             "MUST: Apply with semantic_edit_context(..., apply=True)...",
                             "MUST: Run known_bad_change_context(root, changed_files=[...])..."]
```

### Example 2 — Function Not Found

```
Input:
  target = "lib/reader.py"
  action = "rename_function"
  function = "read_excel_file"   # typo — actual name is "read_excel"
  new_name = "load_excel"

Execution:
  _apply_libcst_transform:
    Walks module looking for FunctionDef named "read_excel_file"
    Not found -> no changes made; returns original_source unchanged
  difflib.unified_diff: additions=0, deletions=0, has_changes=False
  transform_error = None
  function_not_found check:
    action in _FUNCTION_REQUIRED_ACTIONS (rename_function) -> yes
    not has_changes AND not transform_error -> yes
    _function_exists_in_source("read_excel_file") -> False
    -> function_not_found = True

Output:
  metrics.has_changes = False
  metrics.function_not_found = True
  findings = ["WARNING: Function 'read_excel_file' not found in lib/reader.py. No changes possible."]
  risks = ["Target function 'read_excel_file' does not exist in lib/reader.py. Verify the function name..."]
```

## Notable Implementation Patterns

- **`libcst` preferred**: `libcst` preserves blank lines, comments, and formatting.
  The `ast` fallback may reformat code. The `risks` list always includes a warning when
  `ast` is used.
- **`@` normalization**: `decorator = decorator[1:]` strips a leading `@` if the caller
  included it, preventing double-`@` errors regardless of how the caller specifies the
  decorator name.
- **Patch log is append-only**: `.patch_log.jsonl` at project root accumulates one record
  per `apply=True` call. Each record has a short UUID `patch_id`, before/after SHA256
  hashes (16 chars each), and the full diff. This enables rollback by replaying the reverse
  diff or finding the `before_hash` version.
- **AST cache invalidation after apply**: `ast_cache_invalidate(target_path)` is called
  immediately after writing to disk. Without this, subsequent calls that read the AST for
  the same file would see the pre-edit version until the cache entry expired.
- **`function_not_found` is distinct from `transform_error`**: transform_error means the
  AST/libcst transform raised an exception. function_not_found means the transform ran
  cleanly but found no matching function to modify. Both result in `has_changes=False` but
  they have different diagnostic implications.
