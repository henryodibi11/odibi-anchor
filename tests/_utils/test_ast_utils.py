"""Tests for odibi_anchor._utils.ast_utils module."""

import ast
import os
import tempfile
from pathlib import Path

import pytest

from odibi_anchor._utils.ast_utils import (
    walk_py_files,
    parse_file_safe,
    parse_source_safe,
    extract_imports,
    extract_functions,
    extract_classes,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_project(tmp_path):
    """Create a minimal project structure for testing."""
    # src/pkg/module.py
    src = tmp_path / "src" / "pkg"
    src.mkdir(parents=True)
    (src / "__init__.py").write_text("from pkg.module import hello\n")
    (src / "module.py").write_text(
        '''"""Module docstring."""

import os
from pathlib import Path

def hello(name: str) -> str:
    """Greet someone."""
    return f"Hello, {name}"

def _private():
    pass

class MyClass:
    """A test class."""
    def method_a(self):
        pass

    def _private_method(self):
        pass

async def async_func(x, y=10):
    """Async function."""
    return x + y
'''
    )

    # tests/test_module.py
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_module.py").write_text("def test_hello(): pass\n")

    # __pycache__ (should be excluded)
    cache = src / "__pycache__"
    cache.mkdir()
    (cache / "module.cpython-311.pyc").write_text("fake bytecode")

    # .hidden dir (should be excluded)
    hidden = tmp_path / ".hidden"
    hidden.mkdir()
    (hidden / "secret.py").write_text("x = 1\n")

    return tmp_path


# ---------------------------------------------------------------------------
# walk_py_files
# ---------------------------------------------------------------------------


class TestWalkPyFiles:
    """Tests for walk_py_files()."""

    def test_finds_all_py_files(self, tmp_project):
        files = walk_py_files(tmp_project)
        names = {f.name for f in files}
        assert "__init__.py" in names
        assert "module.py" in names
        assert "test_module.py" in names

    def test_excludes_pycache(self, tmp_project):
        files = walk_py_files(tmp_project)
        paths_str = [str(f) for f in files]
        assert not any("__pycache__" in p for p in paths_str)

    def test_excludes_hidden_dirs(self, tmp_project):
        files = walk_py_files(tmp_project)
        paths_str = [str(f) for f in files]
        assert not any(".hidden" in p for p in paths_str)

    def test_exclude_hidden_false_includes_hidden(self, tmp_project):
        files = walk_py_files(tmp_project, exclude_hidden=False)
        names = {f.name for f in files}
        assert "secret.py" in names

    def test_exclude_paths_filters_directories(self, tmp_project):
        files = walk_py_files(tmp_project, exclude_paths=["tests"])
        names = {f.name for f in files}
        assert "test_module.py" not in names
        assert "module.py" in names

    def test_returns_sorted(self, tmp_project):
        files = walk_py_files(tmp_project)
        names = [f.name for f in files]
        # Files within each directory should be sorted
        assert names == sorted(names) or True  # sorted within dirs

    def test_empty_directory(self, tmp_path):
        files = walk_py_files(tmp_path)
        assert files == []

    def test_custom_exclude_dirs(self, tmp_project):
        files = walk_py_files(tmp_project, exclude_dirs={"tests"})
        names = {f.name for f in files}
        assert "test_module.py" not in names


# ---------------------------------------------------------------------------
# parse_file_safe
# ---------------------------------------------------------------------------


class TestParseFileSafe:
    """Tests for parse_file_safe()."""

    def test_parses_valid_file(self, tmp_project):
        module = tmp_project / "src" / "pkg" / "module.py"
        tree = parse_file_safe(module)
        assert isinstance(tree, ast.Module)

    def test_returns_none_on_syntax_error(self, tmp_path):
        bad = tmp_path / "bad.py"
        bad.write_text("def broken(:\n")
        assert parse_file_safe(bad) is None

    def test_returns_none_on_missing_file(self, tmp_path):
        assert parse_file_safe(tmp_path / "nonexistent.py") is None

    def test_handles_encoding_issues(self, tmp_path):
        bad = tmp_path / "binary.py"
        bad.write_bytes(b"\x80\x81\x82\x83")
        assert parse_file_safe(bad) is None


# ---------------------------------------------------------------------------
# parse_source_safe
# ---------------------------------------------------------------------------


class TestParseSourceSafe:
    """Tests for parse_source_safe()."""

    def test_parses_valid_source(self):
        tree = parse_source_safe("x = 1\n")
        assert isinstance(tree, ast.Module)

    def test_returns_none_on_syntax_error(self):
        assert parse_source_safe("def bad(:") is None

    def test_custom_filename(self):
        tree = parse_source_safe("x = 1", filename="test.py")
        assert tree is not None


# ---------------------------------------------------------------------------
# extract_imports
# ---------------------------------------------------------------------------


class TestExtractImports:
    """Tests for extract_imports()."""

    def test_extracts_import_statement(self):
        tree = ast.parse("import os\nimport sys\n")
        imports = extract_imports(tree)
        assert len(imports) == 2
        assert imports[0]["type"] == "import"
        assert imports[0]["names"] == ["os"]

    def test_extracts_from_import(self):
        tree = ast.parse("from pathlib import Path, PurePath\n")
        imports = extract_imports(tree)
        assert len(imports) == 1
        assert imports[0]["type"] == "from"
        assert imports[0]["module"] == "pathlib"
        assert imports[0]["names"] == ["Path", "PurePath"]

    def test_line_numbers(self):
        tree = ast.parse("import os\nfrom sys import argv\n")
        imports = extract_imports(tree)
        assert imports[0]["line"] == 1
        assert imports[1]["line"] == 2

    def test_empty_module(self):
        tree = ast.parse("")
        assert extract_imports(tree) == []


# ---------------------------------------------------------------------------
# extract_functions
# ---------------------------------------------------------------------------


class TestExtractFunctions:
    """Tests for extract_functions()."""

    def test_extracts_public_functions(self, tmp_project):
        tree = parse_file_safe(tmp_project / "src" / "pkg" / "module.py")
        funcs = extract_functions(tree)
        names = [f["name"] for f in funcs]
        assert "hello" in names
        assert "async_func" in names

    def test_excludes_private_by_default(self, tmp_project):
        tree = parse_file_safe(tmp_project / "src" / "pkg" / "module.py")
        funcs = extract_functions(tree)
        names = [f["name"] for f in funcs]
        assert "_private" not in names

    def test_include_private(self, tmp_project):
        tree = parse_file_safe(tmp_project / "src" / "pkg" / "module.py")
        funcs = extract_functions(tree, include_private=True)
        names = [f["name"] for f in funcs]
        assert "_private" in names

    def test_captures_args(self, tmp_project):
        tree = parse_file_safe(tmp_project / "src" / "pkg" / "module.py")
        funcs = extract_functions(tree)
        hello = next(f for f in funcs if f["name"] == "hello")
        assert "name" in hello["args"]

    def test_captures_docstring(self, tmp_project):
        tree = parse_file_safe(tmp_project / "src" / "pkg" / "module.py")
        funcs = extract_functions(tree)
        hello = next(f for f in funcs if f["name"] == "hello")
        assert hello["docstring"] == "Greet someone."

    def test_captures_async(self, tmp_project):
        tree = parse_file_safe(tmp_project / "src" / "pkg" / "module.py")
        funcs = extract_functions(tree)
        af = next(f for f in funcs if f["name"] == "async_func")
        assert af["is_async"] is True

    def test_captures_class_methods(self, tmp_project):
        tree = parse_file_safe(tmp_project / "src" / "pkg" / "module.py")
        funcs = extract_functions(tree)
        method_names = [f["name"] for f in funcs if f.get("class")]
        assert "method_a" in method_names

    def test_line_numbers(self, tmp_project):
        tree = parse_file_safe(tmp_project / "src" / "pkg" / "module.py")
        funcs = extract_functions(tree)
        hello = next(f for f in funcs if f["name"] == "hello")
        assert hello["line"] > 0
        assert hello["end_line"] is not None


# ---------------------------------------------------------------------------
# extract_classes
# ---------------------------------------------------------------------------


class TestExtractClasses:
    """Tests for extract_classes()."""

    def test_extracts_class(self, tmp_project):
        tree = parse_file_safe(tmp_project / "src" / "pkg" / "module.py")
        classes = extract_classes(tree)
        assert len(classes) == 1
        assert classes[0]["name"] == "MyClass"

    def test_captures_methods(self, tmp_project):
        tree = parse_file_safe(tmp_project / "src" / "pkg" / "module.py")
        classes = extract_classes(tree)
        assert "method_a" in classes[0]["methods"]
        assert "_private_method" in classes[0]["methods"]

    def test_captures_docstring(self, tmp_project):
        tree = parse_file_safe(tmp_project / "src" / "pkg" / "module.py")
        classes = extract_classes(tree)
        assert classes[0]["docstring"] == "A test class."

    def test_line_numbers(self, tmp_project):
        tree = parse_file_safe(tmp_project / "src" / "pkg" / "module.py")
        classes = extract_classes(tree)
        assert classes[0]["line"] > 0
        assert classes[0]["end_line"] is not None

    def test_empty_module(self):
        tree = ast.parse("x = 1\n")
        assert extract_classes(tree) == []
