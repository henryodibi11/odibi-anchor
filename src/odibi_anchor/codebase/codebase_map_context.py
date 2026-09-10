"""odibi_anchor.codebase.codebase_map_context — Structural map of a Python codebase.

Scans a Python package directory and produces a compact, structured context
packet describing the full architecture: modules, public functions, classes,
exports, dependency graph, and test coverage mapping.

Designed to eliminate the "orientation phase" — the 5-15 exploratory reads an
AI agent performs at the start of each session to understand what exists, how
it connects, and what patterns are used.

Usage:
    from odibi_anchor.codebase import codebase_map_context

    ctx = codebase_map_context("/path/to/my_package/src/my_package")
    print(ctx["summary"])
    # "my_package: 9 modules, 34 public functions, 8200 lines"

Dependencies: stdlib only (ast, os, pathlib, typing).
"""

from __future__ import annotations

import ast
import os
from pathlib import Path
from typing import Any
from odibi_anchor._utils.contract import validate_output_format
from odibi_anchor._utils.render_utils import render_header_lines, render_metrics_lines, render_bullet_section
from odibi_anchor._utils.ast_utils import read_source_cached, parse_file_safe


def codebase_map_context(
    root: str | Path,
    *,
    subject: str | None = None,
    include_private: bool = False,
    include_tests: bool = True,
    test_dirs: list[str] | None = None,
    max_files: int = 1000,
    max_docstring_chars: int = 500,
    extract_return_keys: bool = True,
    focus_file: str | None = None,
    detect_dead_code: bool = True,
    detail: str = "full",
    output_format: str = "dict",
    frame: "Any | None" = None,
) -> "dict[str, Any] | str":
    """Generate a structural map of a Python codebase.

    Scans all .py files under `root`, parses them with AST, and produces
    a structured context dict describing modules, functions, classes,
    imports, exports, and test coverage.

    Args:
        root: Path to the Python package root directory (the directory
            containing __init__.py or the top-level source folder).
        subject: Human label for the codebase. Defaults to directory name.
        include_private: If True, include functions/classes starting with _.
        include_tests: If True, scan test directories and build test map.
        test_dirs: Directory names to treat as test roots. Defaults to
            ["tests", "test"].
        max_files: Safety cap on number of files to parse. Raises ValueError
            if exceeded.
        max_docstring_chars: Maximum characters to include from function
            docstrings. 0 disables, None means unlimited. Default 500.
        extract_return_keys: If True, extract top-level dict keys from
            return statements (useful for context generators). Default True.
        focus_file: Optional relative path to a single file for deep
            analysis. When set, that file is scanned with include_private=True
            and full detail (constants, all functions, complete docstrings).
            The focused file detail is added to ctx["focus"].
        output_format: "dict" or "markdown".

    Returns:
        Structured context dictionary (or markdown string if output_format="markdown").

    Example:
        >>> from odibi_anchor.codebase import codebase_map_context
        >>> ctx = codebase_map_context("/path/to/my_package")
        >>> ctx["metrics"]["public_function_count"]
        34
    """
    validate_output_format(output_format)

    root = Path(root).resolve()
    if not root.is_dir():
        raise ValueError(f"root must be an existing directory: {root}")

    if test_dirs is None:
        test_dirs = ["tests", "test"]

    subject = subject or root.name

    # Discover all .py files
    py_files = _discover_py_files(root, max_files=max_files)

    # Separate source from test files
    source_files, test_files = _split_source_test(py_files, root, test_dirs)

    # Parse source files
    modules = _parse_modules(
        source_files, root,
        include_private=include_private,
        max_docstring_chars=max_docstring_chars,
        extract_return_keys=extract_return_keys,
    )

    # Parse test files for coverage mapping
    test_map: dict[str, list[str]] = {}
    if include_tests and test_files:
        test_map = _build_test_map(test_files, root, modules)

    # Build dependency graph (internal imports only)
    dependency_graph = _build_dependency_graph(modules, subject)

    # Build reverse dependency graph
    reverse_graph = _build_reverse_graph(dependency_graph)

    # Compute blast radius if focus_file is set
    blast_radius: dict[str, Any] | None = None
    if focus_file:
        blast_radius = _compute_blast_radius(focus_file, reverse_graph)

    # Build exports map from __init__.py files
    exports = _build_exports_map(modules)

    # Dead code detection
    dead_code_candidates: list[dict[str, Any]] = []
    if detect_dead_code:
        dead_code_candidates = _detect_dead_code(modules, reverse_graph, exports)

    # Compute metrics
    metrics = _compute_metrics(modules, test_files, test_map)

    # Build findings and risks
    findings = _build_findings(modules, exports, test_map, metrics)
    risks = _build_risks(modules, test_map, metrics)
    if dead_code_candidates:
        risks.append(
            f"{len(dead_code_candidates)} public function(s) in unreferenced modules "
            "— may be dead code"
        )

    # Build contextual suggestions
    suggested_next_actions = _build_suggestions(
        findings, risks, modules, test_map, reverse_graph,
        dead_code=dead_code_candidates, blast_radius=blast_radius,
    )

    # ── Graph wiring (audit fix) ──

    suggested_next_actions += [

        'SHOULD: Run anchor("known_bad", changed_files=[...]) before making changes.',

        'SHOULD: Run anchor("convention", action="new_function") for naming guidance.',

        'SHOULD: Run anchor("impact", target="file.py") for high-risk modules.',

        'SHOULD: Run anchor("lookup", "function_name") to find framework helpers.',
        "SKILL: Load skills/code-comprehension/SKILL.md for implementation evidence.",

    ]

    # Summary
    summary = (
        f"{subject}: {metrics['module_count']} modules, "
        f"{metrics['public_function_count']} public functions, "
        f"{metrics['total_source_lines']} lines"
    )
    if metrics["test_file_count"] > 0:
        summary += f", {metrics['test_file_count']} test files"

    # Focus file deep-dive
    focus: dict[str, Any] | None = None
    if focus_file:
        focus = _build_focus(root, focus_file, max_docstring_chars=0)
        if blast_radius and focus is not None:
            focus["blast_radius"] = blast_radius

    # Workflow recommendation
    workflow_info = _detect_recommended_workflow(modules, exports, focus_file)

    # detail="summary" (AXI minimal-default-schema): emit per-module counts +
    # names only, ~75% smaller than full signatures/docstrings. The full `modules`
    # is still used above for graph/test_map/dead-code, so nothing downstream
    # loses fidelity — only the returned payload shrinks. Default "full" keeps
    # backward compatibility.
    if detail == "summary":
        output_modules: dict[str, Any] = {
            p: {
                "path": p,
                "lines": m.get("lines", 0),
                "parse_error": m.get("parse_error", False),
                "functions": [f["name"] if isinstance(f, dict) else f for f in m.get("functions", [])],
                "classes": [c["name"] if isinstance(c, dict) else c for c in m.get("classes", [])],
                "n_imports": len(m.get("imports", [])),
            }
            for p, m in modules.items()
        }
    elif detail != "full":
        raise ValueError("detail must be 'full' or 'summary'")
    else:
        output_modules = modules

    # Record the detail level so the output cost-disclosure hint doesn't suggest
    # detail="summary" on a payload that is already a summary.
    metrics["detail"] = detail

    ctx: dict[str, Any] = {
        "kind": "codebase_map_context",
        "subject": subject,
        "summary": summary,
        "metrics": metrics,
        "modules": output_modules,
        "exports": exports,
        "dependency_graph": dependency_graph,
        "reverse_dependency_graph": reverse_graph,
        "dead_code_candidates": dead_code_candidates,
        "focus": focus,
        "test_map": test_map,
        "findings": findings,
        "risks": risks,
        "samples": {},
        "suggested_next_actions": suggested_next_actions,
        "recommended_workflow": workflow_info["recommended_workflow"],
        "workflow_steps_remaining": workflow_info["workflow_steps_remaining"],
    }

    if output_format == "markdown":
        return render_codebase_map_report(ctx)
    return ctx


# ---------------------------------------------------------------------------
# Discovery & Splitting
# ---------------------------------------------------------------------------

def _discover_py_files(root: Path, *, max_files: int) -> list[Path]:
    """Walk the directory tree and collect all .py files."""
    py_files: list[Path] = []
    for dirpath, _dirnames, filenames in os.walk(root):
        # Skip hidden directories and __pycache__
        parts = Path(dirpath).relative_to(root).parts
        if any(p.startswith(".") or p == "__pycache__" for p in parts):
            continue
        for fname in sorted(filenames):
            if fname.endswith(".py"):
                py_files.append(Path(dirpath) / fname)
                if len(py_files) > max_files:
                    raise ValueError(
                        f"Exceeded max_files={max_files}. "
                        f"Pass a higher max_files or narrow the root."
                    )
    return py_files


def _split_source_test(
    py_files: list[Path], root: Path, test_dirs: list[str]
) -> tuple[list[Path], list[Path]]:
    """Separate source files from test files."""
    source: list[Path] = []
    tests: list[Path] = []
    for f in py_files:
        rel = f.relative_to(root)
        # Check if first component is a test directory
        if rel.parts and rel.parts[0] in test_dirs:
            tests.append(f)
        elif f.name.startswith("test_") or f.name.endswith("_test.py"):
            tests.append(f)
        else:
            source.append(f)
    return source, tests


# ---------------------------------------------------------------------------
# AST Parsing
# ---------------------------------------------------------------------------

def _parse_modules(
    files: list[Path], root: Path, *,
    include_private: bool,
    max_docstring_chars: int = 500,
    extract_return_keys: bool = True,
) -> dict[str, Any]:
    """Parse all source files and build module map."""
    modules: dict[str, Any] = {}

    for filepath in files:
        rel = filepath.relative_to(root)
        module_path = str(rel)

        source = read_source_cached(filepath)
        if source is None:
            modules[module_path] = {
                "path": module_path,
                "lines": 0,
                "parse_error": True,
                "functions": [],
                "classes": [],
                "imports": [],
            }
            continue

        tree = parse_file_safe(filepath)
        if tree is None:
            modules[module_path] = {
                "path": module_path,
                "lines": 0,
                "parse_error": True,
                "functions": [],
                "classes": [],
                "imports": [],
            }
            continue

        lines = source.count("\n") + 1
        functions = _extract_functions(
            tree,
            include_private=include_private,
            max_docstring_chars=max_docstring_chars,
            extract_return_keys=extract_return_keys,
        )
        classes = _extract_classes(tree, include_private=include_private)
        imports = _extract_imports(tree)
        module_docstring = ast.get_docstring(tree)
        all_list = _extract_all_list(tree)
        has_main_guard = _has_main_guard(tree)

        modules[module_path] = {
            "path": module_path,
            "lines": lines,
            "parse_error": False,
            "docstring": _truncate(module_docstring, 200) if module_docstring else None,
            "functions": functions,
            "classes": classes,
            "imports": imports,
            "all_list": all_list,
            "has_main_guard": has_main_guard,
        }

    return modules


def _extract_functions(
    tree: ast.Module, *,
    include_private: bool,
    max_docstring_chars: int = 500,
    extract_return_keys: bool = True,
) -> list[dict[str, Any]]:
    """Extract top-level function definitions."""
    functions: list[dict[str, Any]] = []
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not include_private and node.name.startswith("_"):
                continue
            functions.append(_function_info(
                node,
                max_docstring_chars=max_docstring_chars,
                extract_return_keys=extract_return_keys,
            ))
    return functions


def _extract_classes(tree: ast.Module, *, include_private: bool) -> list[dict[str, Any]]:
    """Extract top-level class definitions."""
    classes: list[dict[str, Any]] = []
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ClassDef):
            if not include_private and node.name.startswith("_"):
                continue
            methods = []
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if not include_private and item.name.startswith("_") and item.name != "__init__":
                        continue
                    methods.append(_function_info(item))
            bases = [ast.unparse(b) for b in node.bases] if node.bases else []
            docstring = ast.get_docstring(node)
            end_line = getattr(node, "end_lineno", None)
            classes.append({
                "name": node.name,
                "bases": bases,
                "methods": methods,
                "docstring": _truncate(docstring, 150) if docstring else None,
                "line": node.lineno,
                "end_line": end_line,
            })
    return classes


def _function_info(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    max_docstring_chars: int = 500,
    extract_return_keys: bool = True,
) -> dict[str, Any]:
    """Extract compact info about a function including line range and return keys."""
    # Build signature
    args = node.args
    params: list[str] = []

    # Positional args
    defaults_offset = len(args.args) - len(args.defaults)
    for i, arg in enumerate(args.args):
        if arg.arg == "self" or arg.arg == "cls":
            continue
        p = arg.arg
        if arg.annotation:
            p += f": {ast.unparse(arg.annotation)}"
        if i >= defaults_offset:
            default = args.defaults[i - defaults_offset]
            p += f"={ast.unparse(default)}"
        params.append(p)

    # *args
    if args.vararg:
        params.append(f"*{args.vararg.arg}")
    elif args.kwonlyargs:
        params.append("*")

    # Keyword-only args
    for i, arg in enumerate(args.kwonlyargs):
        p = arg.arg
        if arg.annotation:
            p += f": {ast.unparse(arg.annotation)}"
        _kw_default = args.kw_defaults[i]
        if _kw_default is not None:
            p += f"={ast.unparse(_kw_default)}"
        params.append(p)

    # **kwargs
    if args.kwarg:
        params.append(f"**{args.kwarg.arg}")

    signature = f"({', '.join(params)})"

    # Return annotation
    returns = ast.unparse(node.returns) if node.returns else None

    # Docstring — full (capped by max_docstring_chars)
    docstring = ast.get_docstring(node)
    if docstring and max_docstring_chars > 0:
        doc_text = _truncate(docstring, max_docstring_chars)
    elif docstring and max_docstring_chars == 0:
        # Unlimited
        doc_text = docstring
    else:
        doc_text = None

    # Decorators
    decorators = [ast.unparse(d) for d in node.decorator_list]

    # Line range
    end_line = getattr(node, "end_lineno", None)
    line_count = (end_line - node.lineno + 1) if end_line else None

    # Return keys — extract top-level dict keys from return {...} statements
    return_keys: list[str] | None = None
    if extract_return_keys:
        return_keys = _extract_return_keys(node)

    info: dict[str, Any] = {
        "name": node.name,
        "signature": signature,
        "returns": returns,
        "docstring": doc_text,
        "decorators": decorators if decorators else None,
        "line": node.lineno,
        "end_line": end_line,
        "line_count": line_count,
        "is_async": isinstance(node, ast.AsyncFunctionDef),
    }
    if return_keys:
        info["return_keys"] = return_keys
    return info


def _extract_return_keys(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str] | None:
    """Extract top-level dict keys from return {k: v, ...} statements.

    Walks the function body looking for `return {...}` where the dict uses
    string literal keys. Returns the key list or None if no dict return found.
    """
    for child in ast.walk(node):
        if isinstance(child, ast.Return) and child.value is not None:
            if isinstance(child.value, ast.Dict):
                keys = []
                for key in child.value.keys:
                    if isinstance(key, ast.Constant) and isinstance(key.value, str):
                        keys.append(key.value)
                if keys:
                    return keys
            # Also handle: ctx = {...}; return ctx
            if isinstance(child.value, ast.Name):
                var_name = child.value.id
                # Search backwards for the assignment
                for stmt in ast.walk(node):
                    if isinstance(stmt, ast.Assign):
                        for target in stmt.targets:
                            if isinstance(target, ast.Name) and target.id == var_name:
                                if isinstance(stmt.value, ast.Dict):
                                    keys = []
                                    for key in stmt.value.keys:
                                        if isinstance(key, ast.Constant) and isinstance(key.value, str):
                                            keys.append(key.value)
                                    if keys:
                                        return keys
    return None


def _extract_constants(tree: ast.Module) -> list[dict[str, Any]]:
    """Extract module-level constants (UPPER_CASE assignments)."""
    constants: list[dict[str, Any]] = []
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    name = target.id
                    # Include UPPER_CASE or common patterns
                    if name.isupper() or name.startswith("_") and name[1:].isupper():
                        try:
                            value_repr = ast.unparse(node.value)
                            if len(value_repr) > 200:
                                value_repr = value_repr[:197] + "..."
                        except Exception:
                            value_repr = "..."  # SILENT-OK: AST unparse failure — use placeholder
                        constants.append({
                            "name": name,
                            "value": value_repr,
                            "line": node.lineno,
                        })
    return constants


def _build_focus(
    root: Path, focus_file: str, *, max_docstring_chars: int = 0
) -> dict[str, Any] | None:
    """Deep-dive a single file: all functions (including private), constants, full docstrings."""
    filepath = root / focus_file
    if not filepath.exists():
        return {"error": f"File not found: {focus_file}"}
    if filepath.is_dir():
        # focus_file is a directory — list its Python files instead of parsing
        py_files = sorted(str(p.relative_to(root)) for p in filepath.rglob("*.py") if "__pycache__" not in str(p))
        return {"path": focus_file, "is_directory": True, "python_files": py_files[:50],
                "note": f"Directory with {len(py_files)} Python files. Pass a specific file for deep focus."}

    try:
        source = filepath.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=focus_file)
    except (SyntaxError, UnicodeDecodeError) as e:
        return {"error": f"Parse error: {e}"}

    lines = source.count("\n") + 1
    module_docstring = ast.get_docstring(tree)

    # ALL functions (public + private)
    all_functions: list[dict[str, Any]] = []
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            all_functions.append(_function_info(
                node,
                max_docstring_chars=max_docstring_chars,
                extract_return_keys=True,
            ))

    # ALL classes with all methods
    all_classes: list[dict[str, Any]] = []
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ClassDef):
            methods = []
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    methods.append(_function_info(
                        item,
                        max_docstring_chars=max_docstring_chars,
                        extract_return_keys=True,
                    ))
            bases = [ast.unparse(b) for b in node.bases] if node.bases else []
            docstring = ast.get_docstring(node)
            all_classes.append({
                "name": node.name,
                "bases": bases,
                "methods": methods,
                "docstring": docstring,
                "line": node.lineno,
                "end_line": getattr(node, "end_lineno", None),
            })

    # Constants
    constants = _extract_constants(tree)

    # Imports
    imports = _extract_imports(tree)

    return {
        "path": focus_file,
        "lines": lines,
        "docstring": module_docstring,
        "functions": all_functions,
        "classes": all_classes,
        "constants": constants,
        "imports": imports,
    }


def _extract_imports(tree: ast.Module) -> list[str]:
    """Extract import statements as strings."""
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                imports.append(f"{module}.{alias.name}")
    return imports


def _extract_all_list(tree: ast.Module) -> list[str] | None:
    """Extract __all__ = [...] if present."""
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "__all__":
                    if isinstance(node.value, (ast.List, ast.Tuple)):
                        return [
                            ast.literal_eval(elt)
                            for elt in node.value.elts
                            if isinstance(elt, ast.Constant)
                        ]
    return None


# ---------------------------------------------------------------------------
# Test Mapping
# ---------------------------------------------------------------------------

def _build_test_map(
    test_files: list[Path], root: Path, modules: dict[str, Any]
) -> dict[str, list[str]]:
    """Map source files to their test files based on naming and imports."""
    test_map: dict[str, list[str]] = {}

    # Build lookup: function/module name → source path
    name_to_source: dict[str, str] = {}
    for mod_path, mod_info in modules.items():
        stem = Path(mod_path).stem
        name_to_source[stem] = mod_path
        for func in mod_info.get("functions", []):
            name_to_source[func["name"]] = mod_path

    for test_file in test_files:
        rel = test_file.relative_to(root)
        test_path = str(rel)
        test_stem = test_file.stem

        # Heuristic 1: test_X.py maps to X.py
        if test_stem.startswith("test_"):
            source_stem = test_stem[5:]  # Remove "test_" prefix
            if source_stem in name_to_source:
                src = name_to_source[source_stem]
                test_map.setdefault(src, []).append(test_path)
                continue

        # Heuristic 2: Parse imports in test file
        try:
            source = test_file.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source)
            test_imports = _extract_imports(tree)
            for imp in test_imports:
                parts = imp.split(".")
                for part in parts:
                    if part in name_to_source:
                        src = name_to_source[part]
                        if test_path not in test_map.get(src, []):
                            test_map.setdefault(src, []).append(test_path)
        except (SyntaxError, UnicodeDecodeError):
            pass

    return test_map


# ---------------------------------------------------------------------------
# Dependency Graph
# ---------------------------------------------------------------------------

def _build_dependency_graph(
    modules: dict[str, Any], package_name: str
) -> dict[str, list[str]]:
    """Build internal dependency graph (who imports whom within the package)."""
    graph: dict[str, list[str]] = {}

    # Build stem-to-path lookup
    stem_to_path: dict[str, str] = {}
    for mod_path in modules:
        stem = Path(mod_path).stem
        stem_to_path[stem] = mod_path

    for mod_path, mod_info in modules.items():
        deps: list[str] = []
        for imp in mod_info.get("imports", []):
            # Check if import references another module in this package
            parts = imp.split(".")
            for part in parts:
                if part in stem_to_path and stem_to_path[part] != mod_path:
                    dep = stem_to_path[part]
                    if dep not in deps:
                        deps.append(dep)
            # Also check if it references the package name
            if package_name in imp:
                for stem, path in stem_to_path.items():
                    if stem in imp and path != mod_path and path not in deps:
                        deps.append(path)
        if deps:
            graph[mod_path] = deps

    return graph


def _build_reverse_graph(dependency_graph: "dict[str, list[str]]") -> "dict[str, list[str]]":
    """Build reverse dependency graph: for each module, who imports it.

    Pure inversion of the forward dependency_graph. O(n) over existing data.
    No new parsing required.

    Args:
        dependency_graph: Forward graph mapping importer -> list of dependencies.

    Returns:
        Dict mapping each module to the list of modules that import it.
    """
    reverse: dict[str, list[str]] = {}
    for importer, deps in dependency_graph.items():
        for dep in deps:
            reverse.setdefault(dep, []).append(importer)
    return reverse


def _compute_blast_radius(
    focus_file: str,
    reverse_graph: "dict[str, list[str]]",
) -> "dict[str, Any]":
    """Compute the full set of files affected if focus_file changes.

    Uses BFS over the reverse dependency graph starting from focus_file
    to find all transitive dependents.

    Args:
        focus_file: Relative path of the file being focused on.
        reverse_graph: Reverse dependency graph (module -> list of importers).

    Returns:
        Dict with direct_callers, transitive_callers, total_affected, severity.
    """
    direct = reverse_graph.get(focus_file, [])
    visited = set(direct)
    queue = list(direct)
    while queue:
        node = queue.pop(0)
        for caller in reverse_graph.get(node, []):
            if caller not in visited:
                visited.add(caller)
                queue.append(caller)
    transitive = [f for f in visited if f not in direct]
    total = len(visited)
    severity = "high" if total >= 5 else "medium" if total >= 2 else "low"
    return {
        "direct_callers": direct,
        "transitive_callers": transitive,
        "total_affected": total,
        "severity": severity,
    }



def _has_main_guard(tree: ast.Module) -> bool:
    """Check if module has an `if __name__ == "__main__":` guard.

    Modules with this pattern are CLI entry points meant to be run directly,
    not imported. They should be excluded from dead code detection.
    """
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.If):
            # Check for: __name__ == "__main__" or "__main__" == __name__
            test = node.test
            if isinstance(test, ast.Compare) and len(test.ops) == 1 and isinstance(test.ops[0], ast.Eq):
                left = test.left
                comparator = test.comparators[0]
                # __name__ == "__main__"
                if (isinstance(left, ast.Name) and left.id == "__name__"
                        and isinstance(comparator, ast.Constant) and comparator.value == "__main__"):
                    return True
                # "__main__" == __name__
                if (isinstance(comparator, ast.Name) and comparator.id == "__name__"
                        and isinstance(left, ast.Constant) and left.value == "__main__"):
                    return True
    return False


def _detect_dead_code(
    modules: "dict[str, Any]",
    reverse_graph: "dict[str, list[str]]",
    exports: "dict[str, list[str]]",
) -> "list[dict[str, Any]]":
    """Detect public functions that appear to be unreferenced.

    A function is candidate-dead if:
    - It's public (no leading underscore)
    - Its containing module has no reverse dependencies (nothing imports it)
    - It's not in any __init__.py __all__ list (i.e., not exported)

    This is a heuristic: it checks cross-module import references but does NOT
    do full call-graph analysis (too expensive without a proper call graph).

    Args:
        modules: Parsed module info dict from _parse_modules.
        reverse_graph: Reverse dependency graph.
        exports: Exports map from __init__.py files.

    Returns:
        List of {file, function, reason} dicts for dead-code candidates.
    """
    # Build set of all exported names for quick lookup
    all_exported: set[str] = set()
    for exported_names in exports.values():
        all_exported.update(exported_names)

    dead: list[dict[str, Any]] = []
    for mod_path, mod_info in modules.items():
        # Skip __init__.py files — they're wiring, not dead code
        if Path(mod_path).name == "__init__.py":
            continue
        # Skip modules that have importers
        if mod_path in reverse_graph:
            continue
        # Skip CLI entry points (if __name__ == "__main__":)
        if mod_info.get("has_main_guard"):
            continue
        for fn in mod_info.get("functions", []):
            fn_name = fn["name"] if isinstance(fn, dict) else fn
            if not fn_name.startswith("_") and fn_name not in all_exported:
                dead.append({
                    "file": mod_path,
                    "function": fn_name,
                    "reason": "module has no importers and function is not exported",
                })
    return dead


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

def _build_exports_map(modules: dict[str, Any]) -> dict[str, list[str]]:
    """Build map of what each __init__.py exports."""
    exports: dict[str, list[str]] = {}
    for mod_path, mod_info in modules.items():
        if Path(mod_path).name == "__init__.py":
            all_list = mod_info.get("all_list")
            if all_list:
                exports[mod_path] = all_list
            else:
                # Infer from imports
                imported_names = []
                for imp in mod_info.get("imports", []):
                    parts = imp.split(".")
                    if parts:
                        imported_names.append(parts[-1])
                if imported_names:
                    exports[mod_path] = imported_names
    return exports


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def _compute_metrics(
    modules: dict[str, Any],
    test_files: list[Path],
    test_map: dict[str, list[str]],
) -> dict[str, Any]:
    """Compute summary metrics."""
    total_lines = 0
    public_functions = 0
    private_functions = 0
    class_count = 0
    init_files = 0
    source_files = 0
    parse_errors = 0

    for mod_path, mod_info in modules.items():
        total_lines += mod_info.get("lines", 0)
        if Path(mod_path).name == "__init__.py":
            init_files += 1
        else:
            source_files += 1
        if mod_info.get("parse_error"):
            parse_errors += 1
            continue
        for func in mod_info.get("functions", []):
            if func["name"].startswith("_"):
                private_functions += 1
            else:
                public_functions += 1
        class_count += len(mod_info.get("classes", []))

    # Module count = directories with __init__.py
    module_count = init_files

    # Test coverage
    source_with_tests = len(test_map)
    testable_sources = sum(
        1 for p, m in modules.items()
        if Path(p).name != "__init__.py"
        and not m.get("parse_error")
        and m.get("functions")
    )
    test_coverage_pct = (
        round(source_with_tests / testable_sources, 3)
        if testable_sources > 0 else 0.0
    )

    return {
        "module_count": module_count,
        "source_file_count": source_files,
        "init_file_count": init_files,
        "total_source_lines": total_lines,
        "public_function_count": public_functions,
        "private_function_count": private_functions,
        "class_count": class_count,
        "parse_error_count": parse_errors,
        "test_file_count": len(test_files),
        "source_files_with_tests": source_with_tests,
        "testable_source_files": testable_sources,
        "test_coverage_pct": test_coverage_pct,
    }


# ---------------------------------------------------------------------------
# Findings, Risks, Suggestions
# ---------------------------------------------------------------------------

def _build_findings(
    modules: dict[str, Any],
    exports: dict[str, list[str]],
    test_map: dict[str, list[str]],
    metrics: dict[str, Any],
) -> list[str]:
    """Generate observations about the codebase."""
    findings: list[str] = []

    findings.append(
        f"{metrics['module_count']} packages with "
        f"{metrics['public_function_count']} public functions across "
        f"{metrics['total_source_lines']} lines."
    )

    if metrics["class_count"] > 0:
        findings.append(f"{metrics['class_count']} class(es) defined.")

    if metrics["test_coverage_pct"] >= 0.8:
        findings.append(
            f"Good test coverage: {metrics['test_coverage_pct']:.0%} of "
            f"source files have tests."
        )
    elif metrics["test_file_count"] > 0:
        findings.append(
            f"Partial test coverage: {metrics['source_files_with_tests']}/"
            f"{metrics['testable_source_files']} source files have tests "
            f"({metrics['test_coverage_pct']:.0%})."
        )

    # Largest modules
    sized = [
        (p, m["lines"])
        for p, m in modules.items()
        if Path(p).name != "__init__.py" and not m.get("parse_error")
    ]
    if sized:
        sized.sort(key=lambda x: x[1], reverse=True)
        top = sized[:3]
        findings.append(
            "Largest files: " + ", ".join(f"{Path(p).name} ({n}L)" for p, n in top)
        )

    # Export summary
    total_exports = sum(len(v) for v in exports.values())
    if total_exports:
        findings.append(f"{total_exports} symbols exported via __init__.py files.")

    return findings


def _build_risks(
    modules: dict[str, Any],
    test_map: dict[str, list[str]],
    metrics: dict[str, Any],
) -> list[str]:
    """Identify potential issues."""
    risks: list[str] = []

    if metrics["parse_error_count"] > 0:
        risks.append(
            f"{metrics['parse_error_count']} file(s) had parse errors "
            f"(syntax issues or encoding problems)."
        )

    # Untested files with public functions
    untested = []
    for mod_path, mod_info in modules.items():
        if Path(mod_path).name == "__init__.py":
            continue
        if mod_info.get("parse_error"):
            continue
        if mod_info.get("functions") and mod_path not in test_map:
            untested.append(Path(mod_path).name)
    if untested:
        risks.append(
            f"{len(untested)} source file(s) with public functions have no "
            f"test coverage: {', '.join(untested[:5])}"
        )

    # Very large files (> 500 lines)
    large = [
        Path(p).name
        for p, m in modules.items()
        if m.get("lines", 0) > 500 and Path(p).name != "__init__.py"
    ]
    if large:
        risks.append(
            f"{len(large)} file(s) exceed 500 lines (consider splitting): "
            f"{', '.join(large[:5])}"
        )

    return risks


def _build_suggestions(
    findings: list[str],
    risks: list[str],
    modules: "dict[str, Any]",
    test_map: "dict[str, list[str]]",
    reverse_graph: "dict[str, list[str]]",
    dead_code: "list[dict[str, Any]]| None" = None,
    blast_radius: "dict[str, Any] | None" = None,
) -> list[str]:
    """Generate contextual, analysis-derived suggestions with MUST/SHOULD prefixes.

    Replaces the previous hardcoded boilerplate with file-specific, actionable
    suggestions based on actual codebase analysis results.

    Args:
        findings: List of finding strings from _build_findings.
        risks: List of risk strings from _build_risks.
        modules: Parsed module info dict.
        test_map: Source-to-test file mapping.
        reverse_graph: Reverse dependency graph.
        dead_code: Dead code candidates list (optional).
        blast_radius: Blast radius dict when focus_file is set (optional).

    Returns:
        List of contextual suggestion strings.
    """
    suggestions: list[str] = []

    # Untested modules — point at specific files
    untested = [
        p for p in modules
        if p not in test_map and not Path(p).name.startswith("_")
    ]
    if untested:
        worst = untested[:3]
        suggestions.append(
            f"MUST: Add tests for {len(untested)} untested module(s): "
            + ", ".join(Path(p).name for p in worst)
        )

    # Dead code candidates
    if dead_code:
        suggestions.append(
            f"SHOULD: Review {len(dead_code)} dead-code candidate(s) — "
            f"e.g. {dead_code[0]['file']}:{dead_code[0]['function']}"
        )

    # High blast radius when focus_file given
    if blast_radius and blast_radius.get("severity") == "high":
        suggestions.append(
            f"MUST: anchor('test') after editing — {blast_radius['total_affected']} "
            f"module(s) transitively depend on this file"
        )

    # Highly depended-on modules (targets for extra care)
    hot = sorted(reverse_graph.items(), key=lambda x: len(x[1]), reverse=True)[:2]
    for path, callers in hot:
        if len(callers) >= 3:
            suggestions.append(
                f"SHOULD: anchor('safe') for edits to {Path(path).name} — "
                f"{len(callers)} modules import it"
            )

    # Fallback: if no contextual suggestions generated, add basic workflow hints
    if not suggestions:
        if risks:
            suggestions.append("MUST: Address items in ctx['risks'] to improve codebase health.")

    return suggestions


def _detect_recommended_workflow(modules: dict, exports: dict, focus_file: str | None) -> dict[str, Any]:
    """Detect recommended workflow and remaining steps based on codebase state."""
    # Determine workflow from context
    has_tests = any("test" in path for path in modules)
    has_context_tools = any("_context" in path for path in modules)
    is_focused = focus_file is not None

    if is_focused:
        workflow = "modify_code"
        steps = ["change_impact_context", "implement", "test_focus_context",
                 "consistency_check_context", "save_snapshot"]
    elif has_context_tools:
        workflow = "add_tool"
        steps = ["implement", "test_focus_context", "consistency_check_context",
                 "dogfood_regression_context", "save_snapshot"]
    else:
        workflow = "modify_code"
        steps = ["implement", "test_focus_context", "consistency_check_context",
                 "save_snapshot"]

    return {
        "recommended_workflow": workflow,
        "workflow_steps_remaining": steps,
    }


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _truncate(text: str | None, max_len: int) -> str | None:
    """Truncate text with ellipsis."""
    if text is None:
        return None
    if len(text) <= max_len:
        return text
    return text[:max_len - 3] + "..."


# ---------------------------------------------------------------------------
# Render Function
# ---------------------------------------------------------------------------

def render_codebase_map_report(ctx: dict[str, Any]) -> str:
    """Render a codebase map context dict as a markdown report.

    Args:
        ctx: A dictionary produced by codebase_map_context().

    Returns:
        Human-readable markdown string.

    Raises:
        ValueError: If required keys are missing from ctx.
    """
    required = {"kind", "subject", "summary", "metrics", "modules"}
    if not required.issubset(ctx.keys()):
        missing = required - set(ctx.keys())
        raise ValueError(f"Missing required keys: {sorted(missing)}")

    lines: list[str] = []
    metrics = ctx["metrics"]
    modules = ctx["modules"]

    # Header
    lines.append(f"# Codebase Map: {ctx['subject']}")
    lines.append("")
    lines.append(f"> {ctx['summary']}")
    lines.append("")

    # Metrics table
    lines.append("## Metrics")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("| --- | --- |")
    lines.append(f"| Packages | {metrics['module_count']} |")
    lines.append(f"| Source files | {metrics['source_file_count']} |")
    lines.append(f"| Total lines | {metrics['total_source_lines']} |")
    lines.append(f"| Public functions | {metrics['public_function_count']} |")
    lines.append(f"| Classes | {metrics['class_count']} |")
    lines.append(f"| Test files | {metrics['test_file_count']} |")
    lines.append(f"| Test coverage | {metrics['test_coverage_pct']:.0%} |")
    lines.append("")

    # Module tree
    lines.append("## Modules")
    lines.append("")
    for mod_path, mod_info in sorted(modules.items()):
        if Path(mod_path).name == "__init__.py":
            continue
        if mod_info.get("parse_error"):
            lines.append(f"- `{mod_path}` ⚠️ parse error")
            continue

        func_names = [f["name"] if isinstance(f, dict) else f for f in mod_info.get("functions", [])]
        class_names = [c["name"] if isinstance(c, dict) else c for c in mod_info.get("classes", [])]
        parts = []
        if func_names:
            parts.append(f"fn: {', '.join(func_names[:6])}")
            if len(func_names) > 6:
                parts.append(f"+{len(func_names) - 6} more")
        if class_names:
            parts.append(f"cls: {', '.join(class_names)}")
        detail = f" — {'; '.join(parts)}" if parts else ""
        lines.append(f"- `{mod_path}` ({mod_info['lines']}L){detail}")
    lines.append("")

    # Public API (from exports)
    exports = ctx.get("exports", {})
    if exports:
        lines.append("## Public API (exports)")
        lines.append("")
        for init_path, symbols in sorted(exports.items()):
            pkg = str(Path(init_path).parent)
            lines.append(f"### {pkg}/")
            lines.append("")
            for sym in symbols:
                lines.append(f"- `{sym}`")
            lines.append("")

    # Function signatures (top-level public only, compact).
    # Skipped in detail="summary" mode where functions are name strings, not dicts.
    _has_signatures = any(
        isinstance(f, dict)
        for m in modules.values()
        for f in m.get("functions", [])
    )
    if _has_signatures:
        lines.append("## Function Signatures")
        lines.append("")
        lines.append("| Module | Function | Signature | Returns |")
        lines.append("| --- | --- | --- | --- |")
        for mod_path, mod_info in sorted(modules.items()):
            if Path(mod_path).name == "__init__.py" or mod_info.get("parse_error"):
                continue
            mod_name = Path(mod_path).stem
            for func in mod_info.get("functions", []):
                if not isinstance(func, dict):
                    continue
                sig = func["signature"]
                if len(sig) > 60:
                    sig = sig[:57] + "..."
                ret = func["returns"] or "—"
                if len(ret) > 30:
                    ret = ret[:27] + "..."
                lines.append(f"| {mod_name} | `{func['name']}` | `{sig}` | `{ret}` |")
    lines.append("")

    # Dependency graph
    dep_graph = ctx.get("dependency_graph", {})
    if dep_graph:
        lines.append("## Internal Dependencies")
        lines.append("")
        for source, deps in sorted(dep_graph.items()):
            dep_names = [Path(d).stem for d in deps]
            lines.append(f"- `{Path(source).stem}` → {', '.join(dep_names)}")
        lines.append("")

    # Test map
    test_map = ctx.get("test_map", {})
    if test_map:
        lines.append("## Test Coverage Map")
        lines.append("")
        for source, tests in sorted(test_map.items()):
            test_names = [Path(t).name for t in tests]
            lines.append(f"- `{Path(source).name}` ← {', '.join(test_names)}")
        lines.append("")

    # Findings
    findings = ctx.get("findings", [])
    if findings:
        lines.append("## Findings")
        lines.append("")
        for f in findings:
            lines.append(f"- {f}")
        lines.append("")

    # Risks
    risks = ctx.get("risks", [])
    if risks:
        lines.append("## Risks")
        lines.append("")
        for r in risks:
            lines.append(f"- ⚠️ {r}")
        lines.append("")

    return "\n".join(lines)
