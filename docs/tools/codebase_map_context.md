# codebase_map_context

`codebase_map_context` scans a Python package directory and produces a
structured map of the entire codebase — modules, public functions, classes,
exports, dependency graph, and test coverage mapping.

It answers:

```text
What modules and files exist?
What are the public functions and their signatures?
Who imports whom?
Which files have test coverage?
What does this specific file contain (focus mode)?
```

Designed to eliminate the "orientation phase" — the 5-15 exploratory reads
an AI agent performs at the start of each session.

## Public API

```python
from odibi_anchor.codebase import codebase_map_context

def codebase_map_context(
    root: str | Path,
    *,
    subject: str | None = None,
    include_private: bool = False,
    include_tests: bool = True,
    test_dirs: list[str] | None = None,
    max_files: int = 500,
    max_docstring_chars: int = 500,
    extract_return_keys: bool = True,
    focus_file: str | None = None,
    output_format: str = "dict",
) -> dict[str, Any] | str:
    ...
```

## Parameters

| Parameter | Type | Default | Purpose |
|-----------|------|---------|---------|
| `root` | str or Path | required | Path to package root or project root directory |
| `subject` | str | None | Human label. Defaults to directory name |
| `include_private` | bool | False | Include functions/classes starting with `_` |
| `include_tests` | bool | True | Scan test directories and build test map |
| `test_dirs` | list[str] | `["tests", "test"]` | Directory names treated as test roots |
| `max_files` | int | 500 | Safety cap. Raises ValueError if exceeded |
| `max_docstring_chars` | int | 500 | Max chars from function docstrings. 0 = unlimited |
| `extract_return_keys` | bool | True | Extract top-level dict keys from return statements |
| `focus_file` | str or None | None | Relative path to deep-dive a single file |
| `output_format` | str | `"dict"` | `"dict"` or `"markdown"` |

## Output Shape

```python
{
    "kind": "codebase_map_context",
    "subject": "my_package",
    "summary": "my_package: 9 modules, 34 public functions, 8200 lines, 18 test files",
    "metrics": {
        "module_count": 9,
        "source_file_count": 16,
        "init_file_count": 9,
        "total_source_lines": 8200,
        "public_function_count": 34,
        "private_function_count": 85,
        "class_count": 0,
        "parse_error_count": 0,
        "test_file_count": 18,
        "source_files_with_tests": 12,
        "testable_source_files": 16,
        "test_coverage_pct": 0.75,
    },
    "modules": {
        "validation/quality_gate_context.py": {
            "path": "validation/quality_gate_context.py",
            "lines": 450,
            "parse_error": False,
            "docstring": "Quality gate context generator...",
            "functions": [
                {
                    "name": "quality_gate_context",
                    "signature": "(df: Any, *, keys: list[str] | None=None, target_schema: Any=None, ...)",
                    "returns": "dict[str, Any] | str",
                    "docstring": "Generate a pre-write quality gate context packet.\n\nRuns automatic...",
                    "decorators": None,
                    "line": 87,
                    "end_line": 193,
                    "line_count": 107,
                    "is_async": False,
                    "return_keys": ["kind", "subject", "summary", "metrics", ...],
                },
            ],
            "classes": [],
            "imports": ["odibi_anchor._utils.engine_utils.detect_engine", ...],
            "all_list": None,
        },
        ...
    },
    "exports": {
        "validation/__init__.py": ["quality_gate_context", "render_quality_gate_report", ...],
        ...
    },
    "dependency_graph": {
        "validation/quality_gate_context.py": ["_utils/engine_utils.py"],
        ...
    },
    "focus": {  # Only present when focus_file is set
        "path": "validation/quality_gate_context.py",
        "lines": 2029,
        "docstring": "Full module docstring...",
        "functions": [...],  # ALL functions including private
        "classes": [...],
        "constants": [
            {"name": "_ALL_CHECKS", "value": "('row_count', ...)", "line": 46},
        ],
        "imports": [...],
    },
    "test_map": {
        "validation/quality_gate_context.py": ["tests/validation/test_quality_gate_context.py"],
        ...
    },
    "findings": [...],
    "risks": [...],
    "samples": [],
    "suggested_next_actions": [...],
}
```

### Key Output Fields

- **`metrics["module_count"]`** — Number of packages (directories with `__init__.py`).
- **`metrics["public_function_count"]`** — Total public functions across all source files.
- **`metrics["test_coverage_pct"]`** — Fraction of testable source files that have tests.
- **`modules[path]["functions"]`** — List of function dicts with name, signature, returns, docstring, line range.
- **`exports[init_path]`** — Symbols exported from each `__init__.py`.
- **`dependency_graph[source]`** — Internal files that `source` imports from.
- **`test_map[source]`** — Test files that cover a given source file.
- **`focus`** — Deep-dive on a single file (all privates, constants, unlimited docs). None if not requested.

### Function Dict Fields

Each function in `modules[path]["functions"]` includes:

| Field | Type | Description |
|-------|------|-------------|
| `name` | str | Function name |
| `signature` | str | Full parameter list (not truncated) |
| `returns` | str or None | Return type annotation |
| `docstring` | str or None | Docstring up to `max_docstring_chars` |
| `decorators` | list[str] or None | Decorator names |
| `line` | int | Start line number |
| `end_line` | int or None | End line number |
| `line_count` | int or None | `end_line - line + 1` |
| `is_async` | bool | Whether function is async |
| `return_keys` | list[str] | Top-level dict keys from return statement (only if detected) |

### Focus File Fields

When `focus_file` is set, `ctx["focus"]` contains:

| Field | Type | Description |
|-------|------|-------------|
| `path` | str | Relative path to the file |
| `lines` | int | Total line count |
| `docstring` | str or None | Full module docstring (unlimited) |
| `functions` | list | ALL functions (public + private), unlimited docstrings |
| `classes` | list | ALL classes with all methods |
| `constants` | list | UPPER_CASE module-level assignments with values |
| `imports` | list[str] | All import statements |

## Example Usage

### Quick Orientation

```python
from odibi_anchor.codebase import codebase_map_context

ctx = codebase_map_context("/path/to/my_project")
print(ctx["summary"])
# "my_project: 9 modules, 34 public functions, 8200 lines, 18 test files"

# Find a specific function's line range
for mod in ctx["modules"].values():
    for func in mod.get("functions", []):
        if func["name"] == "quality_gate_context":
            print(func["signature"])
            print(f"Lines {func['line']}-{func['end_line']} ({func['line_count']}L)")
```

### Targeted File Read Using Line Ranges

```python
ctx = codebase_map_context("/path/to/project")

# Instead of reading 2000 lines, read only the function you need:
for mod in ctx["modules"].values():
    for func in mod.get("functions", []):
        if func["name"] == "quality_gate_context":
            # Now do: readFile(path, startLine=87, endLine=193)
            print(f"Read lines {func['line']}-{func['end_line']} only")
```

### Focus File Deep-Dive

```python
ctx = codebase_map_context(
    "/path/to/project",
    focus_file="src/my_package/validation/quality_gate_context.py",
)

focus = ctx["focus"]
print(f"Total functions (including private): {len(focus['functions'])}")
print(f"Constants: {[c['name'] for c in focus['constants']]}")

# Full signatures of private helpers:
for f in focus["functions"]:
    if f["name"].startswith("_"):
        print(f"  {f['name']}{f['signature']}")
```

### Extract Return Dict Keys

```python
ctx = codebase_map_context("/path/to/project")

# Find what a context generator returns:
for mod in ctx["modules"].values():
    for func in mod.get("functions", []):
        if func.get("return_keys"):
            print(f"{func['name']} returns: {func['return_keys']}")
# error_trace_context returns: ['kind', 'subject', 'summary', 'metrics', ...]
```

### Check What's Exported

```python
ctx = codebase_map_context("/path/to/package/src/package")

for init_path, symbols in ctx["exports"].items():
    print(f"{init_path}: {symbols}")
```

### Find Untested Code

```python
ctx = codebase_map_context("/path/to/project")

for mod_path, mod_info in ctx["modules"].items():
    if mod_info.get("functions") and mod_path not in ctx["test_map"]:
        print(f"No tests: {mod_path}")
```

### Impact Analysis (who depends on this file?)

```python
ctx = codebase_map_context("/path/to/project")

target = "validation/quality_gate_context.py"
dependents = [
    src for src, deps in ctx["dependency_graph"].items()
    if target in deps
]
print(f"Files that import {target}: {dependents}")
```

### Markdown Report for LLM Prompt

```python
report = codebase_map_context(
    "/path/to/project",
    subject="my_package",
    output_format="markdown",
)
# Feed to LLM as system context at session start
```

### Standalone Render

```python
from odibi_anchor.codebase import codebase_map_context, render_codebase_map_report

ctx = codebase_map_context("/path/to/project")
print(render_codebase_map_report(ctx))
```

### Source-Only Scan (skip tests)

```python
ctx = codebase_map_context(
    "/path/to/package/src/package",
    include_tests=False,
)
# Faster, no test_map
```

### Include Private Functions

```python
ctx = codebase_map_context(
    "/path/to/project",
    include_private=True,
)
# Shows _helper, _parse, etc. in modules (not just focus)
```

### Control Docstring Length

```python
# Minimal (just function names and signatures)
ctx = codebase_map_context("/path/to/project", max_docstring_chars=0)

# Verbose (full docstrings, good for LLM context)
ctx = codebase_map_context("/path/to/project", max_docstring_chars=2000)
```

## How It Works

1. **Discovery** — Walks the directory tree collecting all `.py` files (skips `__pycache__`, hidden dirs).
2. **Splitting** — Separates source files from test files based on `test_dirs` and file naming patterns.
3. **AST Parsing** — Parses each source file with Python's `ast` module. Extracts functions, classes, imports, `__all__`.
4. **Line Ranges** — Captures `end_lineno` from AST nodes (Python 3.8+) for precise file navigation.
5. **Return Keys** — Extracts top-level dict keys from `return {...}` or `ctx = {...}; return ctx` patterns.
6. **Focus Mode** — When `focus_file` is set, deep-dives that file with all privates, constants, unlimited docs.
7. **Test Mapping** — Maps test files to source files using naming heuristics (`test_X.py` → `X.py`) and import analysis.
8. **Dependency Graph** — Identifies internal imports between modules within the package.
9. **Exports** — Reads `__init__.py` files' `__all__` lists or inferred imports.
10. **Metrics** — Computes summary statistics across all parsed files.

## Performance Notes

- **Zero external dependencies** — stdlib only (ast, os, pathlib).
- **Fast** — Parses ~15K lines in < 2 seconds. AST parsing is the bottleneck.
- **max_files safety** — Prevents accidental scans of huge repos (default 500 files).
- **No execution** — Pure static analysis via AST. Never imports or runs your code.
- **Encoding-tolerant** — Uses `errors="replace"` for non-UTF8 files.
- **Focus mode** — Adds negligible overhead (one extra file parse).

## Choosing `root`

| If your layout is... | Set `root` to... |
|---------------------|-----------------|
| `project/src/pkg/` + `project/tests/` | `project/` (project root) |
| `project/pkg/` (flat) | `project/` |
| Just the source | `project/src/pkg/` (set `include_tests=False`) |

When `root` is the project root, both source and tests are discovered.
When `root` is the package directory, use `include_tests=False` or ensure
test_dirs are siblings.

## When To Use

Use this:

- at the **start of every AI session** to eliminate orientation;
- before adding a new module (see existing patterns);
- before refactoring (understand dependency graph);
- to find untested code;
- to generate LLM system context about a codebase;
- to build custom code review or style checks;
- with `focus_file` before editing a specific module (see all internals).

## Gotchas

- `modules` keys are relative paths from `root`, not absolute.
- `include_private=False` (default) hides `_helper` functions but still counts them in `private_function_count`.
- Test mapping uses heuristics (naming + imports). Non-standard test layouts may have incomplete coverage.
- `max_files=500` is conservative. Large monorepos may need a higher cap.
- Parse errors are captured (not raised) — check `metrics["parse_error_count"]`.
- `dependency_graph` only tracks **internal** dependencies (within the scanned tree).
- `output_format="invalid"` raises `ValueError`.
- `return_keys` extraction is best-effort: works for direct `return {...}` and `ctx = {...}; return ctx`. Functions that delegate to builders won't have keys extracted.
- `end_line` requires Python 3.8+ (available in all Databricks runtimes).
- `focus` is `None` when `focus_file` is not set. Returns `{"error": "..."}` if the file doesn't exist.
- `focus_file` always includes private functions and uses unlimited docstrings regardless of `include_private` and `max_docstring_chars` settings.
