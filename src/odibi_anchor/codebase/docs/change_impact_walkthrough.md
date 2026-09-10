# change_impact_context — Code Walkthrough

## Purpose

`change_impact_context` is the ripple-analysis builder. Before modifying a file or function,
the agent calls this to discover everything that depends on the target: importers, call sites,
test files, documentation, and infrastructure files. It produces a risk assessment and a
prioritised MUST/SHOULD update checklist.

It is invoked by the dispatcher as `anchor("impact", target="path/to/file.py")` and is designed
to replace the manual grep-read-grep cycle that otherwise takes 3-5 tool calls.

## Public Entrypoints

| Name | Signature | Role |
| --- | --- | --- |
| `change_impact_context` | `(root, *, target, function, change_type, subject, include_docs, doc_dirs, include_infra, output_format)` | Main builder |
| `render_change_impact_report` | `(ctx: dict) -> str` | Renders context dict as markdown |

## Change Type Risk Table

| change_type | risk | breaking? |
| --- | --- | --- |
| `add_parameter` | low | No (kwarg with default) |
| `add_required_parameter` | high | Yes |
| `remove_parameter` | high | Yes |
| `rename_parameter` | high | Yes |
| `rename_function` | high | Yes |
| `delete_function` | high | Yes |
| `change_return_type` | medium | Conditional |
| `change_return_shape` | medium | Conditional |
| `add_function` | low | No |
| `modify_logic` | low | No |
| `refactor` | medium | Conditional |

`is_breaking` is True when base_risk is "high", or when base_risk is "medium" and
`call_site_count > 5`.

## Main Execution Flow

```
change_impact_context(root, target="lib/extractor.py",
                      function="extract_excel_structure",
                      change_type="add_parameter")
|
|-- 1. validate_output_format(output_format)
|-- 2. Resolve root; validate target exists within root
|-- 3. _build_search_terms(target_stem, function)
|       -> ["extractor", "extract_excel_structure"]
|-- 4. _find_py_files(root, exclude=target_path)
|-- 5. _find_importers(py_files, root, target_stem, function)
|       for each .py file: AST walk
|         ast.ImportFrom: check module.split(".") contains target_stem
|           with function set: only match if function in imported_names or "*"
|         ast.Import: check alias.name.split(".") contains target_stem
|         _is_init(filepath) -> usage: "re-export" for __init__.py, else "import"
|-- 6. _find_call_sites (only when function is set)
|       regex r"\b{function}\s*(" applied line-by-line via read_source_cached
|       code field truncated to 120 chars
|-- 7. _find_test_references
|       filter: file starts with "test_" | ends with "_test.py" | "tests" in parts
|       check: any search_term in source text
|       count FunctionDef nodes with name.startswith("test_") via ast.walk
|-- 8. _find_doc_references (if include_docs)
|       os.walk over doc_dirs [{".md", ".rst", ".txt", ".yaml", ".yml"}]
|       _find_doc_sections: look backward up to 20 lines for nearest # header; cap at 5
|-- 9. _find_infra_references (if include_infra)
|       active_rules = [r for r in _INFRA_FILES if change_type in r["triggers_on"]]
|       single os.walk (skip hidden + __pycache__) + fnmatch per file path
|-- 10. _assess_risk(base_risk, importer_count, test_count, call_site_count)
|-- 11. _build_checklist -> MUST/SHOULD strings + anchor() workflow hints
`-- 12. ctx dict or render_change_impact_report(ctx)
```

## Helper Responsibilities

| Helper | Responsibility |
| --- | --- |
| `_build_search_terms` | `[target_stem] + ([function] if function else [])` |
| `_find_py_files` | `walk_py_files(root)` excluding target by string comparison |
| `_find_importers` | AST ImportFrom/Import walk; re-export detection via `_is_init` |
| `_find_call_sites` | Regex per line via `read_source_cached`; truncates match to 120 chars |
| `_find_test_references` | Filename filter then term-in-source check; AST count of `test_` defs |
| `_find_doc_references` | `os.walk` over doc_dirs; `_find_doc_sections` caps at 5 headers |
| `_find_infra_references` | Single `os.walk` + `fnmatch`; skips hidden dirs and `__pycache__` |
| `_assess_risk` | Returns `{level, is_breaking, confidence, note, change_type}` |
| `_build_checklist` | MUST/SHOULD per change_type + discovered refs + workflow hints |

## Output Contract

```python
{
    "kind": "change_impact_context",
    "subject": str,
    "summary": str,   # "extract_excel_structure: 3 importer(s), 2 test file(s). LOW risk."
    "metrics": {
        "target": str, "function": str | None, "change_type": str,
        "importer_count": int, "call_site_count": int,
        "test_file_count": int, "doc_reference_count": int,
        "infra_reference_count": int,
        "risk_level": str,     # "low" | "medium" | "high"
        "is_breaking": bool,
    },
    # Builder-specific keys:
    "risk_assessment": {"level": str, "is_breaking": bool,
                        "confidence": str, "note": str, "change_type": str},
    "importers": list[{"file": str, "line": int,
                        "imported_names": list[str], "usage": str}],
    "call_sites": list[{"file": str, "line": int, "code": str}],
    "test_files": list[{"file": str, "matched_terms": list[str], "test_count": int}],
    "doc_references": list[{"file": str, "matched_terms": list[str],
                             "sections": list[str]}],
    "infra_references": list[{"file": str, "label": str, "note": str,
                               "matched_terms": list[str]}],
    "checklist": list[str],
    "findings": list[str],
    "risks": list[str],
    "samples": {},
    "suggested_next_actions": list[str],
}
```

This builder has the richest output shape in the codebase module. `importers`, `call_sites`,
`test_files`, `doc_references`, `infra_references`, `checklist`, and `risk_assessment` are
absent from other context builder outputs.

## Worked Examples

### Example 1 — Add Parameter (Low Risk, Backward-Compatible)

```
Input:
  target = "lib/extractor.py"
  function = "extract_excel_structure"
  change_type = "add_parameter"

Execution:
  search_terms = ["extractor", "extract_excel_structure"]
  _find_importers:
    pipeline/loader.py line 3: "from lib.extractor import extract_excel_structure"
    tests/test_extractor.py line 5: same import
    -> importers = [{..., usage: "import"}, {..., usage: "import"}]
  _find_call_sites:
    regex "\bextract_excel_structure\s*(" matches:
      pipeline/loader.py:42 "result = extract_excel_structure(path, headers=True)"
    -> call_sites = [{file: "pipeline/loader.py", line: 42}]
  _find_test_references:
    tests/test_extractor.py contains "extract_excel_structure" -> matched
    8 test_ functions found via ast.walk
    -> test_files = [{file: "tests/test_extractor.py", test_count: 8}]
  _assess_risk:
    base_risk = "low" (add_parameter), is_breaking = False
    confidence = "medium" (backward-compatible but importers exist)

Output ctx:
  summary = "extract_excel_structure: 2 importer(s), 1 test file(s), 1 call site(s). LOW risk."
  metrics.is_breaking = False
  checklist = [
    "MUST: Update/add tests in 1 test file(s)",
    "MUST: Run anchor('preflight', changed_files=[...]) after all updates ...",
    "MUST: Run anchor('test', changed_files=[...]) to confirm test coverage ...",
  ]
```

### Example 2 — Rename Function with Infrastructure Impact

```
Input:
  target = "lib/models.py"
  function = "build_invoice"
  change_type = "rename_function"

Execution:
  active_rules for "rename_function":
    conftest.py, pyproject.toml, __init__.py, .agent_memory.db, CHANGELOG.md
  _find_infra_references:
    os.walk project once; fnmatch each file path
    lib/__init__.py matches "**/__init__.py" and contains "build_invoice"
    -> infra_refs = [{file: "lib/__init__.py",
                      label: "package exports",
                      note: "Re-exports must be updated",
                      matched_terms: ["build_invoice"]}]
  _find_importers:
    service/invoice_service.py -> usage: "import"
    lib/__init__.py -> _is_init() -> usage: "re-export"
  _assess_risk:
    base_risk = "high" (rename_function), is_breaking = True
  _build_checklist:
    -> [
         "MUST: Update import in service/invoice_service.py (line 2)",
         "MUST: Update import in lib/__init__.py (line 5)",
         "MUST: Update call in service/invoice_service.py (line 18)",
         "MUST: Update re-export in lib/__init__.py",
         "MUST: Check package exports: lib/__init__.py — Re-exports must be updated",
         "MUST: Run anchor('preflight', ...) after all updates ...",
       ]
```

## Notable Implementation Patterns

- **Stdlib-only**: uses `ast`, `os`, `re`, `pathlib` — no third-party dependencies.
- **`read_source_cached`**: all file reads go through a shared cache so a file is never
  read more than once per builder invocation regardless of how many searchers reference it.
- **`fnmatch` for infra, not `glob`**: `glob.glob(..., recursive=True)` is expensive on
  FUSE-mounted Databricks workspace filesystems. A single `os.walk` + `fnmatch` per file
  path is substantially cheaper.
- **`_is_init(filepath)`**: tags `__init__.py` importers as `usage: "re-export"` so the
  checklist can generate a separate update action for package re-export lines.
- **`include_infra=True` default**: infra checks only fire for change types listed in each
  rule's `triggers_on`. A `modify_logic` change produces zero infra references even with
  `include_infra=True` because none of the six rules trigger on it.
- **Checklist always ends with workflow hints**: `anchor("preflight")` and `anchor("test")` are
  appended unconditionally so the output is always actionable as a next-steps list.
