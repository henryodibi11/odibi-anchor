"""Tests for import_resolve_context."""

import json
import pytest

from odibi_anchor.codebase.import_resolve_context import (
    import_resolve_context,
    render_import_resolve_report,
    _file_to_module,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mini_project(tmp_path):
    """Create a minimal project with known definitions and exports."""
    # src/mylib/__init__.py re-exports helper
    src = tmp_path / "src" / "mylib"
    src.mkdir(parents=True)
    init = src / "__init__.py"
    init.write_text(
        'from mylib.utils import helper\n'
        'from mylib.models import MyModel\n\n'
        '__all__ = ["helper", "MyModel"]\n'
    )

    # src/mylib/utils.py defines helper
    utils = src / "utils.py"
    utils.write_text(
        'def helper(x: int) -> int:\n'
        '    return x + 1\n\n'
        'MY_CONSTANT = 42\n'
    )

    # src/mylib/models.py defines MyModel
    models = src / "models.py"
    models.write_text(
        'class MyModel:\n'
        '    pass\n'
    )

    return tmp_path


# ---------------------------------------------------------------------------
# Standard Contract
# ---------------------------------------------------------------------------


class TestStandardContract:
    def test_returns_dict_by_default(self, mini_project):
        ctx = import_resolve_context(str(mini_project), symbol="helper")
        assert isinstance(ctx, dict)

    def test_has_all_standard_fields(self, mini_project):
        ctx = import_resolve_context(str(mini_project), symbol="helper")
        required = {"kind", "subject", "summary", "metrics", "findings",
                     "risks", "samples", "suggested_next_actions"}
        assert required.issubset(ctx.keys())

    def test_kind_is_correct(self, mini_project):
        ctx = import_resolve_context(str(mini_project), symbol="helper")
        assert ctx["kind"] == "import_resolve_context"

    def test_json_serializable(self, mini_project):
        ctx = import_resolve_context(str(mini_project), symbol="helper")
        json.dumps(ctx)

    def test_markdown_output(self, mini_project):
        md = import_resolve_context(str(mini_project), symbol="helper", output_format="markdown")
        assert isinstance(md, str)
        assert "Import Resolution" in md


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


class TestResolution:
    def test_resolves_function(self, mini_project):
        """Finds function definition and generates import."""
        ctx = import_resolve_context(str(mini_project), symbol="helper")
        assert ctx["metrics"]["is_resolved"] is True
        assert "helper" in ctx["resolved_import"]

    def test_prefers_public_api(self, mini_project):
        """Prefers __init__.py re-export over direct module import."""
        ctx = import_resolve_context(str(mini_project), symbol="helper", prefer_public=True)
        # The re-export through __init__ should rank higher
        assert ctx["metrics"]["is_resolved"] is True
        assert ctx["metrics"]["re_exports_found"] > 0

    def test_finds_class(self, mini_project):
        """Resolves class definitions."""
        ctx = import_resolve_context(str(mini_project), symbol="MyModel")
        assert ctx["metrics"]["is_resolved"] is True
        assert "MyModel" in ctx["resolved_import"]

    def test_finds_constant(self, mini_project):
        """Resolves module-level assignments."""
        ctx = import_resolve_context(str(mini_project), symbol="MY_CONSTANT")
        assert ctx["metrics"]["is_resolved"] is True
        assert "MY_CONSTANT" in ctx["resolved_import"]

    def test_not_found(self, mini_project):
        """Symbol not in project returns is_resolved=False."""
        ctx = import_resolve_context(str(mini_project), symbol="nonexistent_symbol")
        assert ctx["metrics"]["is_resolved"] is False
        assert ctx["resolved_import"] == ""

    def test_in_all_list(self, mini_project):
        """is_exported=True when symbol is in __all__."""
        ctx = import_resolve_context(str(mini_project), symbol="helper")
        assert ctx["metrics"]["is_exported"] is True

    def test_not_in_all_list(self, mini_project):
        """is_exported=False when symbol not in __all__."""
        ctx = import_resolve_context(str(mini_project), symbol="MY_CONSTANT")
        assert ctx["metrics"]["is_exported"] is False


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------


class TestFileToModule:
    def test_simple_path(self):
        assert _file_to_module("src/mylib/utils.py") == "mylib.utils"

    def test_init_path(self):
        assert _file_to_module("src/mylib/__init__.py") == "mylib"

    def test_no_src_prefix(self):
        assert _file_to_module("mylib/utils.py") == "mylib.utils"

    def test_nested(self):
        assert _file_to_module("src/pkg/sub/mod.py") == "pkg.sub.mod"
