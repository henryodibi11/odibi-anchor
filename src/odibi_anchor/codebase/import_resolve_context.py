"""odibi_anchor.codebase.import_resolve_context — Import resolution.

Given a symbol name, finds where it's defined in the project and generates
the correct import statement. Eliminates the #1 agent mistake: wrong imports.

Dependencies: stdlib only (ast, pathlib).
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any
from odibi_anchor._utils.contract import validate_output_format, build_base_context
from odibi_anchor._utils.render_utils import render_header_lines, render_metrics_lines, render_bullet_section


def import_resolve_context(
    root: str | Path,
    *,
    symbol: str,
    target_file: str | None = None,
    prefer_public: bool = True,
    subject: str | None = None,
    output_format: str = "dict",
) -> dict[str, Any] | str:
    """Resolve the correct import statement for a symbol.

    Searches the project for where the symbol is defined, checks __init__.py
    re-exports and __all__ lists, and returns the best import path.

    Args:
        root: Project root directory.
        symbol: The symbol name to resolve (function, class, or constant).
        target_file: Optional file that needs the import (used to avoid
            self-imports and prefer relative paths).
        prefer_public: If True, prefer imports through __init__.py re-exports
            over direct module imports. Default True.
        subject: Human label. Defaults to symbol name.
        output_format: "dict" or "markdown".

    Returns:
        Structured context dict (or markdown string).
    """
    validate_output_format(output_format)

    root = Path(root).resolve()
    subject = subject or symbol

    # Single-pass scan: walk once, parse once, extract all signals
    definitions, re_exports, all_entries = _find_all_in_single_pass(root, symbol)

    # Rank candidates
    candidates = _rank_candidates(
        symbol, definitions, re_exports, all_entries,
        target_file=target_file,
        prefer_public=prefer_public,
    )

    # Best candidate
    resolved_import = ""
    if candidates:
        best = candidates[0]
        resolved_import = best["import_statement"]

    metrics = {
        "definitions_found": len(definitions),
        "re_exports_found": len(re_exports),
        "candidates_ranked": len(candidates),
        "is_resolved": len(candidates) > 0,
        "is_exported": len(all_entries) > 0,
    }

    findings = []
    if candidates:
        findings.append(f"Found {len(candidates)} import path(s) for '{symbol}'.")
        findings.append(f"Recommended: `{resolved_import}`")
        if len(candidates) > 1:
            findings.append(f"Alternative: `{candidates[1]['import_statement']}`")
    else:
        findings.append(f"Symbol '{symbol}' not found in the project.")

    risks = []
    if not candidates:
        risks.append(f"'{symbol}' is not defined anywhere under {root.name}/.")
    if candidates and not all_entries:
        risks.append(f"'{symbol}' is not in any __all__ list — may be private.")

    suggested_actions = []
    if resolved_import:
        suggested_actions.append(f"MUST: Use `{resolved_import}`")
        suggested_actions.append(
            "MUST: Run anchor('lookup', 'function_name') for full signature and usage examples."
        )
    else:
        suggested_actions.append(
            f"MUST: Check if '{symbol}' needs to be created or if the name is misspelled."
        )
        suggested_actions.append(
            "MUST: Run anchor('map') to see all available modules and exports."
        )

    summary = (
        f"Resolved: `{resolved_import}`"
        if resolved_import
        else f"Symbol '{symbol}' not found"
    )

    ctx = build_base_context(
        kind="import_resolve_context",
        subject=subject,
        summary=summary,
        metrics=metrics,
        findings=findings,
        risks=risks,
        samples={},
        suggested_next_actions=suggested_actions,
        resolved_import=resolved_import,
        candidates=candidates,
        definitions=definitions,
    )

    if output_format == "markdown":
        return render_import_resolve_report(ctx)
    return ctx


# ---------------------------------------------------------------------------
# Search helpers
# ---------------------------------------------------------------------------



def _find_all_in_single_pass(
    root: Path, symbol: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Single-pass scan: walk files once, parse each once, extract all signals.

    Returns (definitions, re_exports, all_entries) in one pass —
    3x fewer file reads and AST parses compared to separate search functions.
    """
    definitions: list[dict[str, Any]] = []
    re_exports: list[dict[str, Any]] = []
    all_entries: list[dict[str, Any]] = []

    for filepath in _walk_py_files(root, skip_tests=True):
        tree = _parse_file_safe(filepath)
        if tree is None:
            continue
        rel = str(filepath.relative_to(root))
        is_init = filepath.name == "__init__.py"

        for node in ast.walk(tree):
            # Definitions (all files)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name == symbol:
                    definitions.append({
                        "file": rel,
                        "line": node.lineno,
                        "type": "function",
                        "module_path": _file_to_module(rel),
                    })
            elif isinstance(node, ast.ClassDef):
                if node.name == symbol:
                    definitions.append({
                        "file": rel,
                        "line": node.lineno,
                        "type": "class",
                        "module_path": _file_to_module(rel),
                    })
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == symbol:
                        definitions.append({
                            "file": rel,
                            "line": node.lineno,
                            "type": "constant",
                            "module_path": _file_to_module(rel),
                        })
                # __all__ membership (init files only)
                if is_init:
                    for target in node.targets:
                        if isinstance(target, ast.Name) and target.id == "__all__":
                            if isinstance(node.value, ast.List):
                                for elt in node.value.elts:
                                    if isinstance(elt, ast.Constant) and elt.value == symbol:
                                        all_entries.append({
                                            "file": rel,
                                            "module_path": _file_to_module(rel),
                                        })

            # Re-exports (init files only)
            if is_init and isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    name = alias.asname or alias.name
                    if name == symbol:
                        re_exports.append({
                            "file": rel,
                            "line": node.lineno,
                            "from_module": node.module or "",
                            "module_path": _file_to_module(rel),
                        })

    return definitions, re_exports, all_entries


def _find_definitions(root: Path, symbol: str) -> list[dict[str, Any]]:
    """Find files where the symbol is defined (as function, class, or assignment)."""
    definitions = []
    for filepath in _walk_py_files(root):
        tree = _parse_file_safe(filepath)
        if tree is None:
            continue
        rel = str(filepath.relative_to(root))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name == symbol:
                    definitions.append({
                        "file": rel,
                        "line": node.lineno,
                        "type": "function",
                        "module_path": _file_to_module(rel),
                    })
            elif isinstance(node, ast.ClassDef):
                if node.name == symbol:
                    definitions.append({
                        "file": rel,
                        "line": node.lineno,
                        "type": "class",
                        "module_path": _file_to_module(rel),
                    })
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == symbol:
                        definitions.append({
                            "file": rel,
                            "line": node.lineno,
                            "type": "constant",
                            "module_path": _file_to_module(rel),
                        })
    return definitions


def _find_re_exports(root: Path, symbol: str) -> list[dict[str, Any]]:
    """Find __init__.py files that re-export the symbol."""
    re_exports = []
    for filepath in _walk_py_files(root):
        if filepath.name != "__init__.py":
            continue
        tree = _parse_file_safe(filepath)
        if tree is None:
            continue
        rel = str(filepath.relative_to(root))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    name = alias.asname or alias.name
                    if name == symbol:
                        re_exports.append({
                            "file": rel,
                            "line": node.lineno,
                            "from_module": node.module or "",
                            "module_path": _file_to_module(rel),
                        })
    return re_exports


def _find_all_entries(root: Path, symbol: str) -> list[dict[str, Any]]:
    """Find __all__ lists that include the symbol."""
    entries = []
    for filepath in _walk_py_files(root):
        if filepath.name != "__init__.py":
            continue
        tree = _parse_file_safe(filepath)
        if tree is None:
            continue
        rel = str(filepath.relative_to(root))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "__all__":
                        if isinstance(node.value, ast.List):
                            for elt in node.value.elts:
                                val = None
                                if isinstance(elt, ast.Constant):
                                    val = elt.value
                                if val == symbol:
                                    entries.append({
                                        "file": rel,
                                        "module_path": _file_to_module(rel),
                                    })
    return entries


def _rank_candidates(symbol, definitions, re_exports, all_entries,
                     target_file, prefer_public):
    """Rank import candidates by preference."""
    candidates = []

    # From re-exports (public API)
    for rx in re_exports:
        module = rx["module_path"]
        imp = f"from {module} import {symbol}"
        candidates.append({
            "import_statement": imp,
            "module_path": module,
            "source": "re_export",
            "score": 10 if prefer_public else 5,
            "file": rx["file"],
        })

    # From definitions (direct)
    for defn in definitions:
        module = defn["module_path"]
        imp = f"from {module} import {symbol}"
        # Skip if same as target_file
        if target_file and defn["file"] == target_file:
            continue
        candidates.append({
            "import_statement": imp,
            "module_path": module,
            "source": "definition",
            "score": 5 if prefer_public else 10,
            "file": defn["file"],
        })

    # Deduplicate by import_statement
    seen = set()
    unique = []
    for c in candidates:
        if c["import_statement"] not in seen:
            seen.add(c["import_statement"])
            unique.append(c)

    # Sort by score descending
    unique.sort(key=lambda x: x["score"], reverse=True)

    return unique


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------


def _file_to_module(rel_path: str) -> str:
    """Convert a relative file path to a Python module path."""
    path = rel_path.replace("\\", "/")
    if path.startswith("src/"):
        path = path[4:]
    if path.endswith(".py"):
        path = path[:-3]
    if path.endswith("/__init__"):
        path = path[:-9]
    return path.replace("/", ".")


def _walk_py_files(root: Path, *, skip_tests: bool = False) -> list[Path]:
    """Find all .py files under root.

    Args:
        skip_tests: If True, exclude tests/ and scripts/ directories.
            Use for import resolution where test files are unlikely
            to contain importable definitions.
    """
    exclude_dirs = {".git", "__pycache__", ".venv", "venv", "node_modules", ".tox", ".eggs"}
    if skip_tests:
        exclude_dirs.update({"tests", "test", "scripts", "examples", "dogfood_results"})
    py_files = []
    for path in root.rglob("*.py"):
        if not any(part in exclude_dirs for part in path.parts):
            py_files.append(path)
    return py_files


def _parse_file_safe(filepath: Path):
    """Parse a Python file, returning None on failure."""
    try:
        source = filepath.read_text(encoding="utf-8", errors="replace")
        return ast.parse(source)
    except (SyntaxError, UnicodeDecodeError):
        return None


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------


def render_import_resolve_report(ctx: dict[str, Any]) -> str:
    """Render import_resolve_context output as markdown."""
    lines = render_header_lines(ctx, "Import Resolution")
    lines.extend(render_metrics_lines(ctx["metrics"]))

    if ctx.get("resolved_import"):
        lines.extend(["", "## Recommended Import", "",
                       f"```python\n{ctx['resolved_import']}\n```"])

    candidates = ctx.get("candidates", [])
    if len(candidates) > 1:
        lines.extend(["", "## Alternatives", ""])
        for c in candidates[1:]:
            lines.append(f"- `{c['import_statement']}` ({c['source']})")

    lines.extend(render_bullet_section(ctx.get("findings", []), "## Findings"))
    lines.extend(render_bullet_section(ctx.get("risks", []), "## Risks"))

    return "\n".join(lines)
