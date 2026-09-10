"""Tests for semantic_edit_context."""

import json
import pytest
from unittest.mock import patch

from odibi_anchor.codebase.semantic_edit_context import (
    semantic_edit_context,
    render_semantic_edit_report,
    _apply_ast_transform,
    _HAS_LIBCST,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def project_root(tmp_path):
    """Create a minimal project with a target file."""
    src = tmp_path / "src" / "mylib"
    src.mkdir(parents=True)
    target = src / "utils.py"
    target.write_text(
        'import os\nimport sys\n\n\ndef helper(x: int, y: int) -> int:\n'
        '    """Add two numbers."""\n    return x + y\n\n\n'
        'def another_func():\n    return helper(1, 2)\n'
    )
    return tmp_path


@pytest.fixture
def target_rel():
    return "src/mylib/utils.py"


# ---------------------------------------------------------------------------
# Standard Contract
# ---------------------------------------------------------------------------


class TestStandardContract:
    def test_returns_dict_by_default(self, project_root, target_rel):
        ctx = semantic_edit_context(
            str(project_root), target=target_rel, action="add_parameter",
            function="helper", param_name="verbose", param_type="bool",
            param_default="False",
        )
        assert isinstance(ctx, dict)

    def test_has_all_standard_fields(self, project_root, target_rel):
        ctx = semantic_edit_context(
            str(project_root), target=target_rel, action="add_parameter",
            function="helper", param_name="verbose",
        )
        required = {"kind", "subject", "summary", "metrics", "findings",
                     "risks", "samples", "suggested_next_actions"}
        assert required.issubset(ctx.keys())

    def test_kind_is_correct(self, project_root, target_rel):
        ctx = semantic_edit_context(
            str(project_root), target=target_rel, action="add_parameter",
            function="helper", param_name="verbose",
        )
        assert ctx["kind"] == "semantic_edit_context"

    def test_json_serializable(self, project_root, target_rel):
        ctx = semantic_edit_context(
            str(project_root), target=target_rel, action="add_parameter",
            function="helper", param_name="verbose",
        )
        json.dumps(ctx)

    def test_markdown_output(self, project_root, target_rel):
        md = semantic_edit_context(
            str(project_root), target=target_rel, action="add_parameter",
            function="helper", param_name="verbose", output_format="markdown",
        )
        assert isinstance(md, str)
        assert "Semantic Edit" in md


# ---------------------------------------------------------------------------
# AST Fallback Tests (always work without libcst)
# ---------------------------------------------------------------------------


class TestASTFallback:
    """Tests that always use the ast fallback regardless of libcst availability."""

    def test_add_parameter_basic(self, project_root, target_rel):
        """Add a keyword-only param with type and default via ast."""
        with patch("odibi_anchor.codebase.semantic_edit_context._HAS_LIBCST", False):
            ctx = semantic_edit_context(
                str(project_root), target=target_rel, action="add_parameter",
                function="helper", param_name="verbose", param_type="bool",
                param_default="False",
            )
        assert ctx["metrics"]["has_changes"] is True
        assert "verbose" in ctx["diff_preview"]
        assert ctx["metrics"]["lines_added"] > 0

    def test_add_parameter_positional(self, project_root, target_rel):
        """Add a positional param via ast."""
        with patch("odibi_anchor.codebase.semantic_edit_context._HAS_LIBCST", False):
            ctx = semantic_edit_context(
                str(project_root), target=target_rel, action="add_parameter",
                function="helper", param_name="z", param_type="int",
                param_keyword_only=False,
            )
        assert ctx["metrics"]["has_changes"] is True
        assert "z: int" in ctx["modified_source"]

    def test_rename_function(self, project_root, target_rel):
        """Rename function def and references in-file via ast."""
        with patch("odibi_anchor.codebase.semantic_edit_context._HAS_LIBCST", False):
            ctx = semantic_edit_context(
                str(project_root), target=target_rel, action="rename_function",
                function="helper", new_name="add_numbers",
            )
        assert ctx["metrics"]["has_changes"] is True
        assert "add_numbers" in ctx["modified_source"]
        assert "def add_numbers(" in ctx["modified_source"]

    def test_add_import(self, project_root, target_rel):
        """Insert import after last existing import via ast."""
        with patch("odibi_anchor.codebase.semantic_edit_context._HAS_LIBCST", False):
            ctx = semantic_edit_context(
                str(project_root), target=target_rel, action="add_import",
                import_statement="from typing import Optional",
            )
        assert ctx["metrics"]["has_changes"] is True
        assert "from typing import Optional" in ctx["modified_source"]

    def test_remove_import(self, project_root, target_rel):
        """Remove a matching import line via ast."""
        with patch("odibi_anchor.codebase.semantic_edit_context._HAS_LIBCST", False):
            ctx = semantic_edit_context(
                str(project_root), target=target_rel, action="remove_import",
                import_statement="import os",
            )
        assert ctx["metrics"]["has_changes"] is True
        assert "import os" not in ctx["modified_source"]

    def test_unsupported_action_raises_value_error(self, project_root, target_rel):
        """Actions not in _SUPPORTED_ACTIONS raise ValueError upfront."""
        with pytest.raises(ValueError, match="action must be one of"):
            semantic_edit_context(
                str(project_root), target=target_rel, action="convert_to_async",
                function="helper",
            )


# ---------------------------------------------------------------------------
# Dry-run / Apply
# ---------------------------------------------------------------------------


class TestApply:
    def test_dry_run_default(self, project_root, target_rel):
        """apply=False by default, file unchanged."""
        original = (project_root / target_rel).read_text()
        with patch("odibi_anchor.codebase.semantic_edit_context._HAS_LIBCST", False):
            ctx = semantic_edit_context(
                str(project_root), target=target_rel, action="add_parameter",
                function="helper", param_name="verbose",
            )
        assert ctx["metrics"]["applied"] is False
        assert (project_root / target_rel).read_text() == original

    def test_apply_writes_file(self, project_root, target_rel):
        """apply=True writes modified source."""
        with patch("odibi_anchor.codebase.semantic_edit_context._HAS_LIBCST", False):
            ctx = semantic_edit_context(
                str(project_root), target=target_rel, action="add_parameter",
                function="helper", param_name="verbose", apply=True,
            )
        assert ctx["metrics"]["applied"] is True
        new_source = (project_root / target_rel).read_text()
        assert "verbose" in new_source

    def test_diff_preview_format(self, project_root, target_rel):
        """Diff preview uses unified diff format."""
        with patch("odibi_anchor.codebase.semantic_edit_context._HAS_LIBCST", False):
            ctx = semantic_edit_context(
                str(project_root), target=target_rel, action="add_parameter",
                function="helper", param_name="verbose",
            )
        assert "---" in ctx["diff_preview"] or "+++" in ctx["diff_preview"]


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class TestValidation:
    def test_unsupported_action_raises(self, project_root, target_rel):
        """Invalid action raises ValueError."""
        with pytest.raises(ValueError, match="action must be one of"):
            semantic_edit_context(
                str(project_root), target=target_rel, action="nonexistent_action",
                function="helper",
            )

    def test_missing_file_raises(self, project_root):
        """Nonexistent target raises ValueError."""
        with pytest.raises(ValueError, match="target file not found"):
            semantic_edit_context(
                str(project_root), target="nonexistent.py", action="add_parameter",
                function="helper", param_name="x",
            )


# ---------------------------------------------------------------------------
# libcst Tests (skip if not installed)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _HAS_LIBCST, reason="libcst not installed")
class TestLibcst:
    def test_add_parameter_keyword_only(self, project_root, target_rel):
        ctx = semantic_edit_context(
            str(project_root), target=target_rel, action="add_parameter",
            function="helper", param_name="verbose", param_type="bool",
            param_default="False",
        )
        assert ctx["metrics"]["has_changes"] is True
        assert ctx["metrics"]["engine"] == "libcst"

    def test_remove_parameter(self, project_root, target_rel):
        ctx = semantic_edit_context(
            str(project_root), target=target_rel, action="remove_parameter",
            function="helper", param_name="y",
        )
        assert ctx["metrics"]["has_changes"] is True
        # y should be removed from the function def
        assert "y" not in ctx["modified_source"].split("def helper")[1].split(")")[0]

    def test_rename_parameter(self, project_root, target_rel):
        ctx = semantic_edit_context(
            str(project_root), target=target_rel, action="rename_parameter",
            function="helper", param_name="x", new_name="value",
        )
        assert ctx["metrics"]["has_changes"] is True

    def test_add_decorator(self, project_root, target_rel):
        ctx = semantic_edit_context(
            str(project_root), target=target_rel, action="add_decorator",
            function="helper", decorator="staticmethod",
        )
        assert ctx["metrics"]["has_changes"] is True
        assert "@staticmethod" in ctx["modified_source"]

    def test_change_return_type(self, project_root, target_rel):
        ctx = semantic_edit_context(
            str(project_root), target=target_rel, action="change_return_type",
            function="helper", return_type="float",
        )
        assert ctx["metrics"]["has_changes"] is True
        assert "float" in ctx["modified_source"]


# ---------------------------------------------------------------------------
# Regression: Decorator @ prefix fix (v0.4.0)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _HAS_LIBCST, reason="libcst not installed")
class TestDecoratorAtPrefixFix:
    """Regression tests for the bug where add_decorator failed with a parse
    error when the decorator string included the @ prefix.

    Fix: strip leading @ from decorator param before passing to libcst parser.
    """

    def test_decorator_with_at_prefix_works(self, project_root, target_rel):
        """add_decorator should work even if user passes '@staticmethod'."""
        ctx = semantic_edit_context(
            str(project_root), target=target_rel, action="add_decorator",
            function="helper", decorator="@staticmethod",
        )
        assert ctx["metrics"]["has_error"] is False
        assert ctx["metrics"]["has_changes"] is True
        assert "@staticmethod" in ctx["modified_source"]

    def test_decorator_with_at_dotted(self, project_root, target_rel):
        """add_decorator should work with '@functools.lru_cache'."""
        ctx = semantic_edit_context(
            str(project_root), target=target_rel, action="add_decorator",
            function="helper", decorator="@functools.lru_cache",
        )
        assert ctx["metrics"]["has_error"] is False
        assert ctx["metrics"]["has_changes"] is True
        assert "@functools.lru_cache" in ctx["modified_source"]

    def test_decorator_without_at_still_works(self, project_root, target_rel):
        """Decorator without @ (existing behavior) still works fine."""
        ctx = semantic_edit_context(
            str(project_root), target=target_rel, action="add_decorator",
            function="helper", decorator="staticmethod",
        )
        assert ctx["metrics"]["has_error"] is False
        assert "@staticmethod" in ctx["modified_source"]

    def test_remove_decorator_with_at_prefix(self, project_root, target_rel):
        """remove_decorator should also tolerate @ prefix."""
        # First add a decorator
        semantic_edit_context(
            str(project_root), target=target_rel, action="add_decorator",
            function="helper", decorator="staticmethod", apply=True,
        )
        # Then remove it with @ prefix
        ctx = semantic_edit_context(
            str(project_root), target=target_rel, action="remove_decorator",
            function="helper", decorator="@staticmethod",
        )
        assert ctx["metrics"]["has_error"] is False
        assert ctx["metrics"]["has_changes"] is True


# ---------------------------------------------------------------------------
# Regression: add_parameter formatting fix (v0.4.0)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _HAS_LIBCST, reason="libcst not installed")
class TestAddParameterFormattingFix:
    """Regression tests for the bug where add_parameter placed the new param
    on the same line as the closing parenthesis instead of its own line.

    Fix: comma-swap logic copies mid-param comma style to old-last param,
    and assigns old-last's trailing comma to the new param.
    """

    @pytest.fixture
    def multiline_project(self, tmp_path):
        """Create a project with a multi-line function signature."""
        src = tmp_path / "src" / "mylib"
        src.mkdir(parents=True)
        target = src / "service.py"
        target.write_text(
            "from typing import Any\n\n\n"
            "def process(\n"
            "    data: list[str],\n"
            "    *,\n"
            "    verbose: bool = False,\n"
            "    timeout: int = 30,\n"
            ") -> dict[str, Any]:\n"
            "    return {}\n"
        )
        return tmp_path

    def test_new_param_on_own_line(self, multiline_project):
        """New parameter should appear on its own line, not on the closing paren line."""
        ctx = semantic_edit_context(
            str(multiline_project), target="src/mylib/service.py",
            action="add_parameter", function="process",
            param_name="retries", param_type="int", param_default="3",
            param_keyword_only=True,
        )
        assert ctx["metrics"]["has_error"] is False
        assert ctx["metrics"]["has_changes"] is True

        modified = ctx["modified_source"]
        lines = modified.splitlines()

        # Find the line with 'retries'
        retries_lines = [l for l in lines if "retries" in l]
        assert retries_lines, "retries param not found in output"

        retries_line = retries_lines[0]
        # It should NOT also contain ') ->' (that would mean same line as closing paren)
        assert ") ->" not in retries_line, (
            f"New param is on closing-paren line: {retries_line!r}"
        )
        # It should be indented (starts with whitespace)
        assert retries_line.startswith(" ") or retries_line.startswith("\t"), (
            f"New param not indented: {retries_line!r}"
        )

    def test_closing_paren_preserved(self, multiline_project):
        """The closing ') -> ...' should remain on its own line."""
        ctx = semantic_edit_context(
            str(multiline_project), target="src/mylib/service.py",
            action="add_parameter", function="process",
            param_name="retries", param_type="int", param_default="3",
            param_keyword_only=True,
        )
        modified = ctx["modified_source"]
        lines = modified.splitlines()

        # Find line with closing paren and return type
        close_lines = [l for l in lines if ") ->" in l]
        assert close_lines, ") -> not found in modified source"
        # The closing line should NOT contain 'retries'
        assert "retries" not in close_lines[0]

    def test_positional_param_on_own_line(self, tmp_path):
        """Positional params also get their own line in multi-line sigs."""
        src = tmp_path / "src" / "mylib"
        src.mkdir(parents=True)
        target = src / "math.py"
        target.write_text(
            "def add(\n"
            "    x: int,\n"
            "    y: int,\n"
            ") -> int:\n"
            "    return x + y\n"
        )
        ctx = semantic_edit_context(
            str(tmp_path), target="src/mylib/math.py",
            action="add_parameter", function="add",
            param_name="z", param_type="int", param_default="0",
            param_keyword_only=False,
        )
        assert ctx["metrics"]["has_error"] is False
        modified = ctx["modified_source"]
        lines = modified.splitlines()

        z_lines = [l for l in lines if "z" in l and "int" in l]
        assert z_lines, "z param not found"
        assert ") ->" not in z_lines[0], "z param on closing-paren line"


# ---------------------------------------------------------------------------
# Regression: replace_function trailing whitespace
# ---------------------------------------------------------------------------


class TestReplaceFunction:
    """Tests for replace_function action — especially trailing whitespace."""

    def test_no_trailing_whitespace_on_blank_lines(self, project_root, target_rel):
        """replace_function must NOT add trailing whitespace to blank lines."""
        body = '''"""Replacement docstring."""
if x > 0:
    return x

return -x'''
        ctx = semantic_edit_context(
            str(project_root), target=target_rel, action="replace_function",
            function="helper", body=body, apply=True,
        )
        assert ctx["metrics"]["has_changes"]
        assert ctx["metrics"]["applied"]

        # Read the modified file and check for trailing whitespace
        modified = (project_root / target_rel).read_text()
        for i, line in enumerate(modified.split("\n"), 1):
            stripped = line.rstrip()
            assert line == stripped, (
                f"Line {i} has trailing whitespace: {line!r}"
            )
