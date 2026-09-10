"""Shared AST parsing utilities for odibi_anchor codebase tools.

Provides common operations used across codebase_map_context,
change_impact_context, test_focus_context, and consistency_check_context.

Includes mtime-based caching to avoid redundant re-parsing when multiple
tools are called in sequence on the same codebase.
"""

from __future__ import annotations

import ast
import os
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# AST Cache — mtime-based, module-level singleton
# ---------------------------------------------------------------------------

_MAX_CACHE_ENTRIES = 250


class _CacheEntry:
    """Single cached file: source, AST, and extracted metadata."""

    __slots__ = ("mtime", "source", "tree", "_imports", "_functions", "_classes")

    def __init__(self, mtime: float, source: str, tree: ast.Module | None):
        self.mtime = mtime
        self.source = source
        self.tree = tree
        self._imports: list[dict[str, Any]] | None = None
        self._functions: list[dict[str, Any]] | None = None
        self._classes: list[dict[str, Any]] | None = None

    @property
    def imports(self) -> list[dict[str, Any]]:
        if self._imports is None:
            self._imports = extract_imports(self.tree) if self.tree else []
        return self._imports

    @property
    def functions(self) -> list[dict[str, Any]]:
        if self._functions is None:
            self._functions = extract_functions(self.tree) if self.tree else []
        return self._functions

    @property
    def classes(self) -> list[dict[str, Any]]:
        if self._classes is None:
            self._classes = extract_classes(self.tree) if self.tree else []
        return self._classes


class _ASTCache:
    """Module-level LRU cache for parsed Python files.

    Cache invalidation is based on file mtime — if the file's modification
    time changes, the cache entry is evicted and the file is re-parsed.

    Thread safety: NOT thread-safe. Designed for single-threaded agent loops.
    """

    def __init__(self, max_entries: int = _MAX_CACHE_ENTRIES):
        self._store: OrderedDict[str, _CacheEntry] = OrderedDict()
        self._max = max_entries
        self._hits = 0
        self._misses = 0

    def get(self, path: Path) -> _CacheEntry | None:
        """Get cached entry. Skips mtime re-check within same session.

        On workspace FUSE filesystems, stat() and resolve() are extremely
        expensive (~40ms each). Since files don't change mid-agent-session,
        we trust the cache once populated. Call clear() between sessions
        or after file edits to invalidate.
        """
        key = str(path)
        entry = self._store.get(key)
        if entry is None:
            return None
        # Hit — move to end (LRU), no stat check
        self._store.move_to_end(key)
        self._hits += 1
        return entry

    def put(self, path: Path, entry: _CacheEntry) -> None:
        """Store entry, evicting oldest if at capacity."""
        key = str(path)
        self._store[key] = entry
        self._store.move_to_end(key)
        self._misses += 1
        # Evict oldest entries if over capacity
        while len(self._store) > self._max:
            self._store.popitem(last=False)

    def clear(self) -> None:
        """Clear all cached entries."""
        self._store.clear()
        self._hits = 0
        self._misses = 0

    def invalidate(self, path: Path) -> bool:
        """Evict a single file from the cache.

        Call after writing/editing a file so the next read sees fresh content.

        Returns:
            True if the entry was found and evicted, False if not cached.
        """
        key = str(path)
        if key in self._store:
            del self._store[key]
            return True
        return False

    @property
    def stats(self) -> dict[str, int]:
        """Return cache hit/miss statistics."""
        return {
            "entries": len(self._store),
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate_pct": round(
                100 * self._hits / max(1, self._hits + self._misses), 1
            ),
        }


# Module-level singleton
_cache = _ASTCache()


# ---------------------------------------------------------------------------
# Public Cache API
# ---------------------------------------------------------------------------


def ast_cache_clear() -> None:
    """Clear the entire AST file cache. Call between test runs."""
    _cache.clear()


def ast_cache_invalidate(path: str | Path) -> bool:
    """Invalidate a single file in the AST cache.

    Call this after writing or editing a file so subsequent tool calls
    see the updated content. Cheaper than ast_cache_clear() when only
    one file changed.

    Args:
        path: Path to the file that was modified.

    Returns:
        True if the file was in the cache and evicted.
    """
    return _cache.invalidate(Path(path))


def ast_cache_stats() -> dict[str, int]:
    """Return cache hit/miss statistics for diagnostics."""
    return _cache.stats


def get_file_info(path: str | Path) -> dict[str, Any]:
    """Get cached file info: source, tree, imports, functions, classes.

    This is the primary high-level API for tools that need multiple
    pieces of information from a file. It reads, parses, and extracts
    all info in one call with full caching.

    Args:
        path: Path to a .py file.

    Returns:
        Dict with keys: source, tree, imports, functions, classes, line_count.
        All values are cached and reused across tool calls.
        Returns empty/None values if file cannot be read or parsed.
    """
    path = Path(path)
    entry = _get_or_parse(path)
    if entry is None:
        return {
            "source": None,
            "tree": None,
            "imports": [],
            "functions": [],
            "classes": [],
            "line_count": 0,
        }
    return {
        "source": entry.source,
        "tree": entry.tree,
        "imports": entry.imports,
        "functions": entry.functions,
        "classes": entry.classes,
        "line_count": entry.source.count("\n") + 1,
    }


def read_source_cached(path: str | Path) -> str | None:
    """Read file source with caching. Returns None on read failure.

    Use this instead of path.read_text() in tools to benefit from
    the shared cache.
    """
    path = Path(path)
    entry = _get_or_parse(path)
    return entry.source if entry else None


# ---------------------------------------------------------------------------
# File Walking
# ---------------------------------------------------------------------------

_DEFAULT_EXCLUDE_DIRS = {"__pycache__", ".git", ".venv", "node_modules", ".tox", ".mypy_cache"}


def walk_py_files(
    root: str | Path,
    *,
    exclude_dirs: set[str] | None = None,
    exclude_paths: list[str] | None = None,
    exclude_hidden: bool = True,
) -> list[Path]:
    """Recursively find all .py files under root.

    Args:
        root: Directory to walk.
        exclude_dirs: Directory names to skip (added to defaults).
        exclude_paths: Relative path prefixes to skip entirely.
        exclude_hidden: If True, skip directories starting with '.'.

    Returns:
        Sorted list of Path objects for all .py files found.
    """
    root = Path(root)
    skip_dirs = _DEFAULT_EXCLUDE_DIRS | (exclude_dirs or set())
    exclude_prefixes = [p.rstrip("/") for p in (exclude_paths or [])]

    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        # Filter directory names in-place
        dirnames[:] = [
            d for d in dirnames
            if d not in skip_dirs
            and not (exclude_hidden and d.startswith("."))
        ]

        # Check if this directory should be excluded by path prefix
        if exclude_prefixes:
            rel_dir = str(Path(dirpath).relative_to(root))
            if any(rel_dir.startswith(prefix) for prefix in exclude_prefixes):
                dirnames.clear()
                continue

        for fname in sorted(filenames):
            if fname.endswith(".py"):
                fpath = Path(dirpath) / fname
                # Check file-level exclusions
                if exclude_prefixes:
                    rel = str(fpath.relative_to(root))
                    if any(rel.startswith(prefix) for prefix in exclude_prefixes):
                        continue
                files.append(fpath)

    return files


# ---------------------------------------------------------------------------
# Safe Parsing (now cached)
# ---------------------------------------------------------------------------


def parse_file_safe(path: str | Path) -> ast.Module | None:
    """Parse a Python file, returning None on SyntaxError or read failure.

    Results are cached by file mtime — repeated calls with the same
    unmodified file return instantly from cache.

    Args:
        path: Path to the .py file.

    Returns:
        Parsed AST Module, or None if parsing failed.
    """
    path = Path(path)
    entry = _get_or_parse(path)
    return entry.tree if entry else None


def parse_source_safe(source: str, *, filename: str = "<string>") -> ast.Module | None:
    """Parse source code string, returning None on SyntaxError.

    Note: This function does NOT cache (source strings have no stable key).
    For file-based parsing, prefer parse_file_safe() which caches.

    Args:
        source: Python source code.
        filename: Optional filename for error messages.

    Returns:
        Parsed AST Module, or None if parsing failed.
    """
    try:
        return ast.parse(source, filename=filename)
    except SyntaxError:
        return None


# ---------------------------------------------------------------------------
# Import Extraction
# ---------------------------------------------------------------------------


def extract_imports(tree: ast.Module) -> list[dict[str, Any]]:
    """Extract all import statements from an AST tree.

    Returns:
        List of dicts with keys:
        - 'type': 'import' or 'from'
        - 'module': module name (for from-imports)
        - 'names': list of imported names
        - 'line': line number
    """
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.append({
                "type": "import",
                "module": None,
                "names": [alias.name for alias in node.names],
                "line": node.lineno,
            })
        elif isinstance(node, ast.ImportFrom):
            imports.append({
                "type": "from",
                "module": node.module or "",
                "names": [alias.name for alias in node.names],
                "line": node.lineno,
            })
    return imports


# ---------------------------------------------------------------------------
# Function & Class Extraction
# ---------------------------------------------------------------------------


def extract_functions(
    tree: ast.Module,
    *,
    include_private: bool = False,
    include_nested: bool = False,
) -> list[dict[str, Any]]:
    """Extract function definitions from an AST tree.

    Args:
        tree: Parsed AST module.
        include_private: If True, include functions starting with '_'.
        include_nested: If True, include functions nested inside other functions/classes.

    Returns:
        List of dicts with keys:
        - 'name': function name
        - 'line': start line number
        - 'end_line': end line number (or None)
        - 'args': list of argument names
        - 'decorators': list of decorator names
        - 'docstring': first-line docstring or None
        - 'is_async': bool
    """
    functions = []
    nodes = ast.iter_child_nodes(tree) if not include_nested else ast.walk(tree)

    for node in nodes:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not include_private and node.name.startswith("_"):
                continue
            functions.append(_func_info(node))
        elif isinstance(node, ast.ClassDef) and not include_nested:
            # Extract methods from top-level classes
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if not include_private and child.name.startswith("_"):
                        continue
                    info = _func_info(child)
                    info["class"] = node.name
                    functions.append(info)

    return functions


def extract_classes(tree: ast.Module) -> list[dict[str, Any]]:
    """Extract class definitions from an AST tree.

    Returns:
        List of dicts with keys:
        - 'name': class name
        - 'line': start line number
        - 'end_line': end line number
        - 'bases': list of base class names
        - 'methods': list of method names
        - 'docstring': first-line docstring or None
    """
    classes = []
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ClassDef):
            methods = [
                n.name for n in ast.iter_child_nodes(node)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            ]
            classes.append({
                "name": node.name,
                "line": node.lineno,
                "end_line": getattr(node, "end_lineno", None),
                "bases": [_name_from_node(b) for b in node.bases],
                "methods": methods,
                "docstring": ast.get_docstring(node, clean=True),
            })
    return classes


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _get_or_parse(path: Path) -> _CacheEntry | None:
    """Core cache lookup + populate. Returns None if file unreadable."""
    # Check cache first
    entry = _cache.get(path)
    if entry is not None:
        return entry

    # Cache miss — read and parse
    try:
        mtime = os.path.getmtime(path)
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None

    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        tree = None

    entry = _CacheEntry(mtime=mtime, source=source, tree=tree)
    _cache.put(path, entry)
    return entry


def _func_info(node: ast.FunctionDef | ast.AsyncFunctionDef) -> dict[str, Any]:
    """Build info dict for a function/method node."""
    args = []
    for arg in node.args.args + node.args.posonlyargs + node.args.kwonlyargs:
        args.append(arg.arg)
    if node.args.vararg:
        args.append(f"*{node.args.vararg.arg}")
    if node.args.kwarg:
        args.append(f"**{node.args.kwarg.arg}")

    decorators = []
    for dec in node.decorator_list:
        decorators.append(_name_from_node(dec))

    return {
        "name": node.name,
        "line": node.lineno,
        "end_line": getattr(node, "end_lineno", None),
        "args": args,
        "decorators": decorators,
        "docstring": ast.get_docstring(node, clean=True),
        "is_async": isinstance(node, ast.AsyncFunctionDef),
    }


def _name_from_node(node: ast.expr) -> str:
    """Best-effort name extraction from an AST expression node."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_name_from_node(node.value)}.{node.attr}"
    if isinstance(node, ast.Call):
        return _name_from_node(node.func)
    if isinstance(node, ast.Subscript):
        return _name_from_node(node.value)
    return ast.dump(node)
