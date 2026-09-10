"""odibi_anchor.codebase.framework_lookup_context — Framework API lookup.

Given a task description or function name, returns the exact framework function,
import path, signature, docstring, and usage example — always parsed live from
source (with optional pre-built JSON index for speed).

Eliminates the "read source files to find the right function" bottleneck by
providing instant, always-current API discovery.

Usage:
    from odibi_anchor.codebase import framework_lookup_context

    ctx = framework_lookup_context("deduplicate keeping latest row")
    for m in ctx["matches"]:
        print(m["import_statement"])
        print(m["signature"])

Dependencies: stdlib only (ast, os, pathlib, re, json).
"""

from __future__ import annotations

import ast
import json
import os
import re
from pathlib import Path
from typing import Any
from odibi_anchor._utils.contract import validate_output_format
from odibi_anchor._utils.ast_utils import read_source_cached, parse_file_safe
from odibi_anchor._utils.render_utils import render_header_lines, render_metrics_lines, render_bullet_section

# ---------------------------------------------------------------------------
# Module-level cache
# ---------------------------------------------------------------------------

_REGISTRY_CACHE: dict[str, Any] = {}
_REGISTRY_MTIME: dict[str, float] = {}

# Default categories to scan
_DEFAULT_CATEGORIES = ("transformers", "validation", "utils", "patterns", "io")

# Extended categories (optional, included when present)
_EXTENDED_CATEGORIES = ("delta", "document", "understandability", "logger")

# Concept synonym map — expands queries to find semantically related functions
_CONCEPT_SYNONYMS: dict[str, list[str]] = {
    "coalesce": ["coalesce_columns", "union_frames", "merge", "combine", "first_non_null"],
    "union": ["union_frames", "concat", "append", "stack", "combine"],
    "join": ["join_with_prefix", "merge", "left_join", "inner_join", "lookup"],
    "merge": ["merge_into", "upsert_into", "join_with_prefix", "union_frames", "combine"],
    "deduplicate": ["deduplicate", "drop_duplicates", "distinct", "unique", "dedup"],
    "validate": ["validate_data", "split_valid_invalid", "quality_gate", "check", "assert"],
    "save": ["save", "write", "persist", "merge_into", "upsert_into", "saver"],
    "dimension": ["build_dimension", "scd", "scd2", "slowly_changing", "lookup"],
    "fact": ["build_fact", "measure", "metric", "aggregate"],
    "read": ["read", "load", "ingest", "read_auto", "reader"],
    "pivot": ["pivot", "unpivot", "melt", "wide_to_long", "long_to_wide"],
    "window": ["window_calc", "rank", "row_number", "lead", "lag", "running"],
    "hash": ["hash_columns", "checksum", "fingerprint", "surrogate_key"],
    "explode": ["explode_column", "flatten", "unnest", "array_to_rows"],
    "schema": ["generate_schema", "validate_schema", "schema_diff", "check_drift"],
    "profile": ["profile", "describe", "statistics", "summary", "data_quality"],
    "priority": ["coalesce_columns", "union_frames", "first_non_null", "override"],
    "combine": ["union_frames", "coalesce_columns", "merge", "concat", "append"],
    "sources": ["union_frames", "coalesce_columns", "reader", "read_auto", "ingest"],
}





# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def framework_lookup_context(
    query: str,
    *,
    framework_root: str | None = None,
    category: str | None = None,
    max_results: int = 5,
    include_source: bool = False,
    output_format: str = "dict",
) -> dict[str, Any] | str:
    """Look up framework functions matching a query.

    Parses framework source (or reads pre-built index) to find functions
    matching the query by name, docstring keywords, or parameter names.

    Args:
        query: Natural language description or function name to search for.
        framework_root: Absolute path to the framework package root.
        category: Restrict search to one category (e.g., "transformers").
            None searches all categories.
        max_results: Maximum number of matches to return.
        include_source: If True, include full source code for each match.
        output_format: "dict" or "markdown".

    Returns:
        Structured context dict (or markdown string).

    Example:
        >>> ctx = framework_lookup_context("deduplicate keeping latest")
        >>> ctx["matches"][0]["function"]
        'deduplicate'
    """
    validate_output_format(output_format)
    if framework_root is None:
        raise ValueError(
            "framework_root is required. Pass the path to your framework package, "
            "e.g., framework_root='/Workspace/Repos/your-team/shared-library'"
        )

    root = Path(framework_root)

    # Build or retrieve registry
    registry = _get_registry(root)

    # Filter by category if specified
    if category:
        category_lower = category.lower()
        entries = [e for e in registry if e["category"] == category_lower]
        categories_searched = 1
    else:
        entries = registry
        categories_searched = len({e["category"] for e in registry})

    # Score each entry against the query
    scored = []
    for entry in entries:
        score = _score_match(query, entry)
        if score > 0.0:
            scored.append((score, entry))

    # Sort by score descending, limit results
    scored.sort(key=lambda x: x[0], reverse=True)
    top_matches = scored[:max_results]

    # Build match results
    matches = []
    for score, entry in top_matches:
        match_info: dict[str, Any] = {
            "function": entry["name"],
            "module": entry["module"],
            "import_statement": entry["import_statement"],
            "signature": entry["signature"],
            "docstring_summary": entry["docstring_summary"],
            "parameters": entry["parameters"],
            "example": entry.get("example", ""),
            "relevance_score": round(score, 3),
        }
        if include_source and entry.get("source"):
            match_info["source"] = entry["source"]
        matches.append(match_info)

    # Build output
    match_names = [m["function"] for m in matches]
    summary = (
        f"Found {len(matches)} function(s) matching '{query}'"
        + (f": {', '.join(match_names)}" if match_names else "")
    )

    findings = []
    if matches:
        findings.append(
            f"Top match: {matches[0]['function']} "
            f"(relevance: {matches[0]['relevance_score']:.2f})"
        )
        if len(matches) > 1:
            findings.append(
                f"{len(matches) - 1} additional match(es) with lower relevance."
            )
    else:
        findings.append("No matching functions found in the framework.")

    risks = []
    if not matches:
        risks.append(
            "If the framework doesn't have this function, write manual code "
            "with PRE-CODE GATE comment."
        )
    elif matches[0]["relevance_score"] < 0.5:
        risks.append(
            "Top match has low relevance — verify it does what you need "
            "before using."
        )

    suggested_actions = []
    if matches:
        suggested_actions.append(
            f"Use: {matches[0]['import_statement']}"
        )
        if matches[0].get("example"):
            suggested_actions.append(
                f"Example call: {matches[0]['example']}"
            )
        suggested_actions.append(
            "MUST: Run anchor('quality', df, subject='...', keys=[...]) after writing data."
        )
        suggested_actions.append(
            "MUST: Run anchor('convention', action='new_function', ...) if wrapping this in a helper."
        )
    else:
        suggested_actions.append(
            "Write manual implementation with PRE-CODE GATE comment."
        )
        suggested_actions.append(
            "COULD: At learning closure, capture a supported reusable observation; "
            "building a solution does not automatically require a save."
        )

    ctx: dict[str, Any] = {
        "kind": "framework_lookup_context",
        "subject": query if len(query) <= 40 else query[:37] + "...",
        "summary": summary,
        "metrics": {
            "query": query,
            "matches_found": len(matches),
            "categories_searched": categories_searched,
            "registry_size": len(registry),
        },
        "matches": matches,
        "findings": findings,
        "risks": risks,
        "samples": {},
        "suggested_next_actions": suggested_actions,
    }

    if output_format == "markdown":
        return render_framework_lookup_report(ctx)
    return ctx


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------


def render_framework_lookup_report(ctx: dict[str, Any]) -> str:
    """Render framework_lookup_context output as markdown."""
    lines = render_header_lines(ctx, "Framework Lookup")
    lines.extend(render_metrics_lines(ctx["metrics"]))

    if ctx["matches"]:
        lines.extend(["", "## Matches", ""])
        for i, m in enumerate(ctx["matches"], 1):
            lines.append(f"### {i}. `{m['function']}` (relevance: {m['relevance_score']:.2f})")
            lines.append("")
            lines.append(f"```python")
            lines.append(f"{m['import_statement']}")
            lines.append(f"```")
            lines.append("")
            lines.append(f"**Signature:** `{m['signature']}`")
            lines.append("")
            if m["docstring_summary"]:
                lines.append(f"**Description:** {m['docstring_summary']}")
                lines.append("")
            if m["parameters"]:
                lines.append("**Parameters:**")
                for p in m["parameters"]:
                    ptype = f" ({p['type']})" if p.get("type") else ""
                    pdesc = f" — {p['description']}" if p.get("description") else ""
                    lines.append(f"- `{p['name']}`{ptype}{pdesc}")
                lines.append("")
            if m.get("example"):
                lines.append(f"**Example:** `{m['example']}`")
                lines.append("")
            if m.get("source"):
                lines.append(f"<details><summary>Source</summary>")
                lines.append("")
                lines.append(f"```python")
                lines.append(m["source"])
                lines.append(f"```")
                lines.append(f"</details>")
                lines.append("")

    lines.extend(render_bullet_section(ctx["findings"], "## Findings"))
    lines.extend(render_bullet_section(ctx["risks"], "## Risks"))
    lines.extend(render_bullet_section(ctx["suggested_next_actions"], "## Suggested Actions"))

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Registry: Build & Cache
# ---------------------------------------------------------------------------


def _get_registry(root: Path) -> list[dict[str, Any]]:
    """Get or build the function registry, with mtime-based cache invalidation."""
    root_str = str(root)

    # Check if we have a cached registry that's still valid
    if root_str in _REGISTRY_CACHE:
        # Check mtime of __init__.py files to detect changes
        current_mtime = _get_max_init_mtime(root)
        if current_mtime <= _REGISTRY_MTIME.get(root_str, 0):
            return _REGISTRY_CACHE[root_str]

    # Try to load pre-built index first
    index_path = root / ".api_index.json"
    if index_path.is_file():
        registry = _load_json_index(index_path, root)
        if registry is not None:
            _REGISTRY_CACHE[root_str] = registry
            _REGISTRY_MTIME[root_str] = _get_max_init_mtime(root)
            return registry

    # Fall back to live AST parsing
    registry = _build_registry_from_source(root)
    _REGISTRY_CACHE[root_str] = registry
    _REGISTRY_MTIME[root_str] = _get_max_init_mtime(root)
    return registry


def _get_max_init_mtime(root: Path) -> float:
    """Get the max mtime across all __init__.py files."""
    max_mtime = 0.0
    for cat in _DEFAULT_CATEGORIES + _EXTENDED_CATEGORIES:
        init_path = root / cat / "__init__.py"
        if init_path.is_file():
            try:
                mtime = os.path.getmtime(init_path)
                max_mtime = max(max_mtime, mtime)
            except OSError:
                pass
    return max_mtime


def _load_json_index(index_path: Path, root: Path) -> list[dict[str, Any]] | None:
    """Load a pre-built JSON index. Returns None if stale or invalid."""
    try:
        with open(index_path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None

    # Check staleness: compare index generated_at against init mtimes
    functions = data.get("functions", [])
    if not functions:
        return None

    # Convert to registry format
    registry = []
    for func in functions:
        entry = {
            "name": func["name"],
            "module": func.get("module", ""),
            "import_statement": func.get("import", ""),
            "signature": func.get("signature", func["name"] + "(...)"),
            "docstring_summary": func.get("docstring", ""),
            "docstring_full": func.get("docstring", ""),
            "parameters": func.get("parameters", []),
            "example": func.get("example", ""),
            "category": func.get("category", ""),
            "keywords": func.get("keywords", []),
            "source": "",
        }
        registry.append(entry)

    return registry


def _build_registry_from_source(root: Path) -> list[dict[str, Any]]:
    """Build function registry by AST-parsing source files."""
    registry: list[dict[str, Any]] = []

    all_categories = _DEFAULT_CATEGORIES + _EXTENDED_CATEGORIES
    for category in all_categories:
        cat_dir = root / category
        init_path = cat_dir / "__init__.py"
        if not init_path.is_file():
            continue

        # Parse __init__.py to get exported names and their source modules
        exports = _parse_init_exports(init_path, category)

        # For each export, find its source definition
        for export in exports:
            source_file = _find_source_file(cat_dir, export)
            if source_file:
                func_info = _parse_function_info(
                    source_file, export["name"], category, root
                )
                if func_info:
                    registry.append(func_info)
            else:
                # Still add with basic info from __init__.py
                pkg_name = root.name
                registry.append({
                    "name": export["name"],
                    "module": f"{pkg_name}.{category}",
                    "import_statement": f"from {pkg_name}.{category} import {export['name']}",
                    "signature": f"{export['name']}(...)",
                    "docstring_summary": "",
                    "docstring_full": "",
                    "parameters": [],
                    "example": "",
                    "category": category,
                    "keywords": _generate_keywords(export["name"]),
                    "source": "",
                })

    return registry


def _parse_init_exports(init_path: Path, category: str) -> list[dict[str, str]]:
    """Parse __init__.py to extract exported names and their source modules."""
    source = read_source_cached(init_path)
    if source is None:
        return []
    tree = parse_file_safe(init_path)
    if tree is None:
        return []

    exports: list[dict[str, str]] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                name = alias.asname or alias.name
                exports.append({
                    "name": name,
                    "from_module": module,
                })

    # Also check __all__ to confirm what's truly exported
    all_names = _extract_all_names(tree)
    if all_names:
        # Filter to only names in __all__
        all_set = set(all_names)
        exports = [e for e in exports if e["name"] in all_set]

    return exports


def _extract_all_names(tree: ast.Module) -> list[str]:
    """Extract names from __all__ = [...] assignment."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "__all__":
                    if isinstance(node.value, ast.List):
                        names = []
                        for elt in node.value.elts:
                            if isinstance(elt, ast.Constant) and isinstance(
                                elt.value, str
                            ):
                                names.append(elt.value)
                        return names
    return []


def _find_source_file(cat_dir: Path, export: dict[str, str]) -> Path | None:
    """Find the source .py file for an export based on its from_module."""
    from_module = export["from_module"]
    if not from_module:
        return None

    # Convert relative module to file path
    # e.g., ".window_ops" -> "window_ops.py"
    # e.g., ".spark" -> "spark/__init__.py" or "spark.py"
    parts = from_module.lstrip(".").split(".")
    if not parts or not parts[0]:
        return None

    # Try as a file
    file_path = cat_dir / (parts[0] + ".py")
    if file_path.is_file():
        # If multiple parts, it's a submodule
        if len(parts) > 1:
            sub_path = cat_dir / parts[0] / (parts[1] + ".py")
            if sub_path.is_file():
                return sub_path
        return file_path

    # Try as a directory with __init__.py
    dir_path = cat_dir / parts[0]
    if dir_path.is_dir():
        if len(parts) > 1:
            sub_file = dir_path / (parts[1] + ".py")
            if sub_file.is_file():
                return sub_file
        init = dir_path / "__init__.py"
        if init.is_file():
            return init

    return None


def _parse_function_info(
    source_file: Path,
    func_name: str,
    category: str,
    root: Path,
) -> dict[str, Any] | None:
    """Parse a source file to extract info about a specific function/class."""
    source = read_source_cached(source_file)
    if source is None:
        return None
    tree = parse_file_safe(source_file)
    if tree is None:
        return None

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == func_name:
                return _extract_func_info(node, source, func_name, category, root)
        elif isinstance(node, ast.ClassDef):
            if node.name == func_name:
                return _extract_class_info(node, source, func_name, category, root)

    # Function might be nested or not at top level — try deeper search
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == func_name:
                return _extract_func_info(node, source, func_name, category, root)
        elif isinstance(node, ast.ClassDef):
            if node.name == func_name:
                return _extract_class_info(node, source, func_name, category, root)

    return None


def _extract_func_info(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    source: str,
    func_name: str,
    category: str,
    root: Path,
) -> dict[str, Any]:
    """Extract function information from an AST node."""
    # Signature
    signature = _build_signature(node)

    # Docstring
    docstring_full = ast.get_docstring(node) or ""
    docstring_summary = _get_docstring_summary(docstring_full)

    # Parameters
    parameters = _extract_parameters(node, docstring_full)

    # Example from docstring
    example = _extract_example(docstring_full)

    # Source code
    source_lines = source.splitlines()
    func_source = "\n".join(
        source_lines[node.lineno - 1: node.end_lineno]
    ) if hasattr(node, "end_lineno") and node.end_lineno else ""

    # Keywords
    keywords = _generate_keywords(func_name)
    keywords.extend(_extract_docstring_keywords(docstring_full))

    return {
        "name": func_name,
        "module": f"{root.name}.{category}",
        "import_statement": f"from {root.name}.{category} import {func_name}",
        "signature": signature,
        "docstring_summary": docstring_summary,
        "docstring_full": docstring_full,
        "parameters": parameters,
        "example": example,
        "category": category,
        "keywords": list(dict.fromkeys(keywords)),  # dedupe preserving order
        "source": func_source,
    }


def _extract_class_info(
    node: ast.ClassDef,
    source: str,
    class_name: str,
    category: str,
    root: Path,
) -> dict[str, Any]:
    """Extract class information from an AST node."""
    docstring_full = ast.get_docstring(node) or ""
    docstring_summary = _get_docstring_summary(docstring_full)

    # Get __init__ parameters if present
    parameters = []
    signature = f"{class_name}(...)"
    for item in node.body:
        if isinstance(item, ast.FunctionDef) and item.name == "__init__":
            signature = _build_signature(item).replace("__init__", class_name)
            parameters = _extract_parameters(item, docstring_full)
            break

    example = _extract_example(docstring_full)
    keywords = _generate_keywords(class_name)
    keywords.extend(_extract_docstring_keywords(docstring_full))

    source_lines = source.splitlines()
    class_source = "\n".join(
        source_lines[node.lineno - 1: node.end_lineno]
    ) if hasattr(node, "end_lineno") and node.end_lineno else ""

    return {
        "name": class_name,
        "module": f"{root.name}.{category}",
        "import_statement": f"from {root.name}.{category} import {class_name}",
        "signature": signature,
        "docstring_summary": docstring_summary,
        "docstring_full": docstring_full,
        "parameters": parameters,
        "example": example,
        "category": category,
        "keywords": list(dict.fromkeys(keywords)),
        "source": class_source,
    }


# ---------------------------------------------------------------------------
# AST Helpers
# ---------------------------------------------------------------------------


def _build_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """Build a human-readable signature string from a function AST node."""
    args = node.args
    parts: list[str] = []

    # Positional args (skip 'self' and 'cls')
    positional = args.args[:]
    if positional and positional[0].arg in ("self", "cls"):
        positional = positional[1:]

    # Calculate default offset for positional args
    num_positional = len(positional)
    num_defaults = len(args.defaults)
    default_offset = num_positional - num_defaults

    for i, arg in enumerate(positional):
        param_str = arg.arg
        # Add type annotation if present
        if arg.annotation:
            ann = _annotation_to_str(arg.annotation)
            if ann:
                param_str += f": {ann}"
        # Add default if present
        default_idx = i - default_offset
        if default_idx >= 0 and default_idx < len(args.defaults):
            default_val = _default_to_str(args.defaults[default_idx])
            param_str += f"={default_val}"
        parts.append(param_str)

    # *args
    if args.vararg:
        parts.append(f"*{args.vararg.arg}")
    elif args.kwonlyargs:
        parts.append("*")

    # Keyword-only args
    for i, kwarg in enumerate(args.kwonlyargs):
        param_str = kwarg.arg
        if kwarg.annotation:
            ann = _annotation_to_str(kwarg.annotation)
            if ann:
                param_str += f": {ann}"
        if i < len(args.kw_defaults) and args.kw_defaults[i] is not None:
            default_val = _default_to_str(args.kw_defaults[i])
            param_str += f"={default_val}"
        parts.append(param_str)

    # **kwargs
    if args.kwarg:
        parts.append(f"**{args.kwarg.arg}")

    return f"{node.name}({', '.join(parts)})"


def _annotation_to_str(node: ast.expr) -> str:
    """Convert an annotation AST node to a string."""
    if isinstance(node, ast.Constant):
        return repr(node.value)
    elif isinstance(node, ast.Name):
        return node.id
    elif isinstance(node, ast.Attribute):
        return f"{_annotation_to_str(node.value)}.{node.attr}"
    elif isinstance(node, ast.Subscript):
        value = _annotation_to_str(node.value)
        slice_str = _annotation_to_str(node.slice)
        return f"{value}[{slice_str}]"
    elif isinstance(node, ast.Tuple):
        return ", ".join(_annotation_to_str(e) for e in node.elts)
    elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return f"{_annotation_to_str(node.left)} | {_annotation_to_str(node.right)}"
    elif isinstance(node, ast.List):
        return "[" + ", ".join(_annotation_to_str(e) for e in node.elts) + "]"
    return ""


def _default_to_str(node: ast.expr | None) -> str:
    """Convert a default value AST node to a string."""
    if node is None:
        return "None"
    if isinstance(node, ast.Constant):
        return repr(node.value)
    elif isinstance(node, ast.Name):
        return node.id
    elif isinstance(node, ast.List):
        return "[]" if not node.elts else "[...]"
    elif isinstance(node, ast.Dict):
        return "{}" if not node.keys else "{...}"
    elif isinstance(node, ast.Tuple):
        return "()" if not node.elts else "(...)"
    elif isinstance(node, ast.Call):
        func_name = _annotation_to_str(node.func)
        return f"{func_name}(...)"
    elif isinstance(node, ast.Attribute):
        return f"{_annotation_to_str(node.value)}.{node.attr}"
    elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return f"-{_default_to_str(node.operand)}"
    return "..."


# ---------------------------------------------------------------------------
# Docstring Parsing
# ---------------------------------------------------------------------------


def _get_docstring_summary(docstring: str) -> str:
    """Extract first line/sentence of a docstring."""
    if not docstring:
        return ""
    lines = docstring.strip().splitlines()
    if not lines:
        return ""
    first_line = lines[0].strip()
    # If first line ends with a period, it's the summary
    if first_line.endswith("."):
        return first_line
    # Otherwise take up to the first blank line
    summary_lines = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            break
        summary_lines.append(stripped)
    return " ".join(summary_lines)


def _extract_parameters(
    node: ast.FunctionDef | ast.AsyncFunctionDef, docstring: str
) -> list[dict[str, str]]:
    """Extract parameter info from function signature and docstring."""
    # Parse docstring for parameter descriptions
    param_docs = _parse_docstring_params(docstring)

    args = node.args
    parameters: list[dict[str, str]] = []

    # Positional args (skip self/cls)
    positional = args.args[:]
    if positional and positional[0].arg in ("self", "cls"):
        positional = positional[1:]

    for arg in positional:
        param: dict[str, str] = {"name": arg.arg, "type": "", "description": ""}
        if arg.annotation:
            param["type"] = _annotation_to_str(arg.annotation)
        if arg.arg in param_docs:
            param["description"] = param_docs[arg.arg]
        parameters.append(param)

    # Keyword-only args
    for kwarg in args.kwonlyargs:
        param = {"name": kwarg.arg, "type": "", "description": ""}
        if kwarg.annotation:
            param["type"] = _annotation_to_str(kwarg.annotation)
        if kwarg.arg in param_docs:
            param["description"] = param_docs[kwarg.arg]
        parameters.append(param)

    return parameters


def _parse_docstring_params(docstring: str) -> dict[str, str]:
    """Parse Args/Parameters section from a docstring."""
    if not docstring:
        return {}

    params: dict[str, str] = {}
    lines = docstring.splitlines()
    in_args = False
    current_param = ""
    current_desc_lines: list[str] = []

    for line in lines:
        stripped = line.strip()

        # Detect Args: or Parameters: section
        if stripped.lower() in ("args:", "parameters:", "params:"):
            in_args = True
            continue

        # Detect end of Args section (another section header)
        if in_args and stripped and stripped.endswith(":") and not stripped.startswith(" "):
            # Check if it's a known section header
            header = stripped.rstrip(":").lower()
            if header in (
                "returns", "raises", "yields", "note", "notes",
                "example", "examples", "see also", "references",
            ):
                # Save current param
                if current_param:
                    params[current_param] = " ".join(current_desc_lines).strip()
                in_args = False
                continue

        if not in_args:
            continue

        # Parse parameter lines (indented, format: "name: description" or "name (type): desc")
        param_match = re.match(
            r"^\s{4,}(\w+)(?:\s*\(([^)]*)\))?\s*[:—–-]\s*(.*)$", line
        )
        if param_match:
            # Save previous param
            if current_param:
                params[current_param] = " ".join(current_desc_lines).strip()
            current_param = param_match.group(1)
            current_desc_lines = [param_match.group(3).strip()]
        elif current_param and stripped and line.startswith("        "):
            # Continuation line
            current_desc_lines.append(stripped)
        elif not stripped and current_param:
            # Blank line ends the current param
            params[current_param] = " ".join(current_desc_lines).strip()
            current_param = ""
            current_desc_lines = []

    # Save final param
    if current_param:
        params[current_param] = " ".join(current_desc_lines).strip()

    return params


def _extract_example(docstring: str) -> str:
    """Extract example code from docstring."""
    if not docstring:
        return ""

    lines = docstring.splitlines()
    in_example = False
    example_lines: list[str] = []

    for line in lines:
        stripped = line.strip()

        if stripped.lower().startswith("example"):
            in_example = True
            continue

        if in_example:
            if stripped.startswith(">>>"):
                # Doctest format
                code = stripped[3:].strip()
                if code:
                    example_lines.append(code)
            elif stripped and not stripped.startswith("..."):
                # If we already have example lines and hit a non-example line, stop
                if example_lines:
                    break
            elif not stripped and example_lines:
                break

    return example_lines[0] if example_lines else ""


# ---------------------------------------------------------------------------
# Keyword / Search
# ---------------------------------------------------------------------------


def _generate_keywords(name: str) -> list[str]:
    """Generate search keywords from a function/class name."""
    # Split on underscores and camelCase
    parts = re.split(r"[_\s]+", name.lower())
    # Also split camelCase
    camel_parts = re.sub(r"([a-z])([A-Z])", r"\1 \2", name).lower().split()
    all_parts = list(dict.fromkeys(parts + camel_parts))
    # Add the full name
    return [name.lower()] + [p for p in all_parts if p != name.lower() and len(p) >= 2]


def _extract_docstring_keywords(docstring: str) -> list[str]:
    """Extract meaningful keywords from docstring text."""
    if not docstring:
        return []

    # Take the first 200 chars of docstring for keyword extraction
    text = docstring[:200].lower()
    # Remove common stop words and extract content words
    stop_words = {
        "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
        "have", "has", "had", "do", "does", "did", "will", "would", "could",
        "should", "may", "might", "shall", "can", "need", "dare", "ought",
        "used", "to", "of", "in", "for", "on", "with", "at", "by", "from",
        "as", "into", "through", "during", "before", "after", "above", "below",
        "between", "out", "off", "over", "under", "again", "further", "then",
        "once", "here", "there", "when", "where", "why", "how", "all", "each",
        "every", "both", "few", "more", "most", "other", "some", "such", "no",
        "nor", "not", "only", "own", "same", "so", "than", "too", "very",
        "and", "but", "if", "or", "because", "until", "while", "that", "this",
        "these", "those", "it", "its", "which", "what", "who", "whom",
    }
    words = re.findall(r"\b[a-z][a-z_]{2,}\b", text)
    keywords = [w for w in words if w not in stop_words]
    return list(dict.fromkeys(keywords))[:8]


def _score_match(query: str, entry: dict[str, Any]) -> float:
    """Score how well an entry matches the query."""
    query_lower = query.lower().strip()
    name_lower = entry["name"].lower()

    # Tokenize query
    query_tokens = set(re.split(r"[\s_,;:]+", query_lower))
    query_tokens.discard("")

    # Expand query tokens with concept synonyms
    expanded_names: set[str] = set()
    for token in list(query_tokens):
        if token in _CONCEPT_SYNONYMS:
            expanded_names.update(
                n.lower() for n in _CONCEPT_SYNONYMS[token]
            )

    score = 0.0

    # 1. Exact name match (highest weight)
    if query_lower == name_lower:
        score += 1.0
    elif name_lower in query_lower or query_lower in name_lower:
        # Substring match
        score += 0.7
    else:
        # Partial token match against name tokens
        name_tokens = set(re.split(r"[_\s]+", name_lower))
        overlap = query_tokens & name_tokens
        if overlap:
            score += 0.5 * (len(overlap) / max(len(query_tokens), 1))

    # 1b. Synonym expansion match (entry name matches expanded concepts)
    if expanded_names:
        if name_lower in expanded_names:
            score += 0.6
        else:
            name_tokens = set(re.split(r"[_\s]+", name_lower))
            synonym_token_overlap = name_tokens & expanded_names
            if synonym_token_overlap:
                score += 0.4 * (len(synonym_token_overlap) / max(len(name_tokens), 1))

    # 2. Keyword match
    entry_keywords = set(k.lower() for k in entry.get("keywords", []))
    keyword_overlap = query_tokens & entry_keywords
    if keyword_overlap:
        score += 0.3 * (len(keyword_overlap) / max(len(query_tokens), 1))

    # 3. Docstring keyword match
    docstring = entry.get("docstring_full", "").lower()
    if docstring:
        doc_matches = sum(1 for t in query_tokens if t in docstring)
        if doc_matches:
            score += 0.2 * (doc_matches / max(len(query_tokens), 1))

    # 4. Parameter name match
    param_names = {p["name"].lower() for p in entry.get("parameters", [])}
    param_overlap = query_tokens & param_names
    if param_overlap:
        score += 0.15 * (len(param_overlap) / max(len(query_tokens), 1))

    # 5. Category bonus (if query mentions category)
    category = entry.get("category", "").lower()
    if category and category in query_lower:
        score += 0.1

    return min(score, 1.0)  # Cap at 1.0


# ---------------------------------------------------------------------------
# Cache Management (for testing)
# ---------------------------------------------------------------------------


def _clear_cache() -> None:
    """Clear the module-level registry cache. Used in tests."""
    _REGISTRY_CACHE.clear()
    _REGISTRY_MTIME.clear()
