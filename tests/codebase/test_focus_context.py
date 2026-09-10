"""Tests for test_focus_context."""

import os
import tempfile
from pathlib import Path

import pytest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from odibi_anchor.codebase.focus_context import (
    test_focus_context as run_test_focus,
    render_test_focus_report as run_render_report,
    _path_to_module_name,
    _parse_source_definitions,
    _parse_test_references,
    _collect_test_files,
    _collect_source_files,
    _find_coverage_gaps,
    _suggest_test_cases,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_project(tmp_path):
    """A minimal project with source and test files."""
    # Source files
    src = tmp_path / "src" / "mylib"
    src.mkdir(parents=True)
    (src / "__init__.py").write_text(
        "from mylib.transform import process_data, render_transform_report\n"
    )
    (src / "transform.py").write_text(
        "BATCH_SIZE = 100\n\n"
        "_MAX_RETRIES = 3\n\n"
        "def process_data(df, *, mode=\"fast\"):\n"
        "    \"\"\"Process a dataframe.\"\"\"\n"
        "    return df\n\n"
        "def render_transform_report(ctx):\n"
        "    \"\"\"Render report.\"\"\"\n"
        "    return f\"# {ctx}\"\n\n"
        "def _helper(x):\n"
        "    return x + 1\n"
    )
    (src / "utils.py").write_text(
        "TIMEOUT = 30\n\n"
        "def clean_string(s):\n"
        "    \"\"\"Clean a string.\"\"\"\n"
        "    return s.strip()\n\n"
        "def validate_input(x):\n"
        "    \"\"\"Validate input.\"\"\"\n"
        "    return bool(x)\n"
    )

    # Test files
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "__init__.py").write_text("")
    (tests / "test_transform.py").write_text(
        "import sys\n"
        "from mylib.transform import process_data\n\n"
        "def test_process_data_basic():\n"
        "    assert process_data({}) == {}\n\n"
        "def test_process_data_with_mode():\n"
        "    result = process_data({}, mode=\"slow\")\n"
        "    assert result == {}\n\n"
        "def test_batch_size_constant():\n"
        "    from mylib.transform import BATCH_SIZE\n"
        "    assert BATCH_SIZE == 100\n"
    )
    (tests / "test_utils.py").write_text(
        "from mylib.utils import clean_string\n\n"
        "def test_clean_string():\n"
        "    assert clean_string(\" hello \") == \"hello\"\n"
    )

    return tmp_path


@pytest.fixture
def empty_project(tmp_path):
    """A project with no test files."""
    src = tmp_path / "src" / "mylib"
    src.mkdir(parents=True)
    (src / "__init__.py").write_text("")
    (src / "core.py").write_text(
        "def main():\n    pass\n"
    )
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "__init__.py").write_text("")
    return tmp_path


# ---------------------------------------------------------------------------
# Output Contract Tests
# ---------------------------------------------------------------------------

class TestOutputContract:
    """Test that output follows standard contract."""

    def test_has_required_keys(self, sample_project):
        ctx = run_test_focus(sample_project, changed_files=["src/mylib/transform.py"])
        required = {"kind", "subject", "summary", "metrics", "findings", "risks",
                    "samples", "suggested_next_actions"}
        assert required.issubset(set(ctx.keys()))

    def test_kind_is_correct(self, sample_project):
        ctx = run_test_focus(sample_project, changed_files=["src/mylib/transform.py"])
        assert ctx["kind"] == "test_focus_context"

    def test_subject_defaults_to_dirname(self, sample_project):
        ctx = run_test_focus(sample_project)
        assert ctx["subject"] == sample_project.name

    def test_subject_can_be_overridden(self, sample_project):
        ctx = run_test_focus(sample_project, subject="my_project")
        assert ctx["subject"] == "my_project"

    def test_metrics_has_expected_fields(self, sample_project):
        ctx = run_test_focus(sample_project, changed_files=["src/mylib/transform.py"])
        expected_metrics = {
            "changed_functions_count", "affected_test_files_count",
            "coverage_gaps_count", "estimated_run_seconds", "test_functions_in_scope",
        }
        assert expected_metrics.issubset(set(ctx["metrics"].keys()))

    def test_has_focus_specific_fields(self, sample_project):
        ctx = run_test_focus(sample_project, changed_files=["src/mylib/transform.py"])
        focus_fields = {"affected_test_files", "coverage_gaps", "run_command",
                        "suggested_test_cases"}
        assert focus_fields.issubset(set(ctx.keys()))


# ---------------------------------------------------------------------------
# Affected Test File Discovery
# ---------------------------------------------------------------------------

class TestAffectedTestFiles:
    """Test detection of affected test files."""

    def test_finds_test_by_import(self, sample_project):
        ctx = run_test_focus(sample_project, changed_files=["src/mylib/transform.py"])
        paths = [a["path"] for a in ctx["affected_test_files"]]
        assert "tests/test_transform.py" in paths

    def test_finds_test_by_function_reference(self, sample_project):
        ctx = run_test_focus(sample_project, changed_functions=["process_data"])
        paths = [a["path"] for a in ctx["affected_test_files"]]
        assert "tests/test_transform.py" in paths

    def test_finds_test_by_name_convention(self, sample_project):
        ctx = run_test_focus(sample_project, changed_files=["src/mylib/utils.py"])
        paths = [a["path"] for a in ctx["affected_test_files"]]
        assert "tests/test_utils.py" in paths

    def test_no_false_positives(self, sample_project):
        """Changes to utils should not flag test_transform."""
        ctx = run_test_focus(sample_project, changed_files=["src/mylib/utils.py"])
        paths = [a["path"] for a in ctx["affected_test_files"]]
        assert "tests/test_transform.py" not in paths

    def test_empty_when_no_tests_reference(self, empty_project):
        ctx = run_test_focus(empty_project, changed_files=["src/mylib/core.py"])
        assert ctx["affected_test_files"] == []


# ---------------------------------------------------------------------------
# Coverage Gap Detection
# ---------------------------------------------------------------------------

class TestCoverageGaps:
    """Test coverage gap identification."""

    def test_detects_untested_function(self, sample_project):
        """validate_input in utils.py is not referenced by any test."""
        ctx = run_test_focus(sample_project, changed_files=["src/mylib/utils.py"])
        gap_funcs = [g["function"] for g in ctx["coverage_gaps"]]
        assert "validate_input" in gap_funcs

    def test_detects_untested_constant(self, sample_project):
        """TIMEOUT in utils.py is not referenced by any test."""
        ctx = run_test_focus(sample_project, changed_files=["src/mylib/utils.py"])
        gap_funcs = [g["function"] for g in ctx["coverage_gaps"]]
        assert "TIMEOUT" in gap_funcs

    def test_private_functions_excluded_from_gaps(self, sample_project):
        """_helper is private and should not appear as a gap."""
        ctx = run_test_focus(sample_project, changed_files=["src/mylib/transform.py"])
        gap_funcs = [g["function"] for g in ctx["coverage_gaps"]]
        assert "_helper" not in gap_funcs

    def test_tested_functions_not_in_gaps(self, sample_project):
        """process_data is tested, so it should not be a gap."""
        ctx = run_test_focus(sample_project, changed_files=["src/mylib/transform.py"])
        gap_funcs = [g["function"] for g in ctx["coverage_gaps"]]
        assert "process_data" not in gap_funcs

    def test_tested_constants_not_in_gaps(self, sample_project):
        """BATCH_SIZE is referenced in tests, so not a gap."""
        ctx = run_test_focus(sample_project, changed_files=["src/mylib/transform.py"])
        gap_funcs = [g["function"] for g in ctx["coverage_gaps"]]
        assert "BATCH_SIZE" not in gap_funcs


# ---------------------------------------------------------------------------
# Run Command Generation
# ---------------------------------------------------------------------------

class TestRunCommand:
    """Test run command generation."""

    def test_targeted_command_when_affected(self, sample_project):
        ctx = run_test_focus(sample_project, changed_files=["src/mylib/transform.py"])
        assert "test_transform.py" in ctx["run_command"]
        assert ctx["run_command"].startswith("pytest ")

    def test_full_suite_when_no_affected(self, empty_project):
        ctx = run_test_focus(empty_project, changed_files=["src/mylib/core.py"])
        assert ctx["run_command"] == "pytest tests/ -q"


# ---------------------------------------------------------------------------
# Suggested Test Cases
# ---------------------------------------------------------------------------

class TestSuggestedCases:
    """Test test case suggestion generation."""

    def test_suggests_for_public_functions(self, sample_project):
        ctx = run_test_focus(sample_project, changed_functions=["process_data"])
        assert len(ctx["suggested_test_cases"]) > 0
        assert any("process_data" in tc for tc in ctx["suggested_test_cases"])

    def test_suggests_for_constants(self, sample_project):
        ctx = run_test_focus(sample_project, changed_functions=["BATCH_SIZE"])
        suggestions = ctx["suggested_test_cases"]
        assert any("batch_size" in tc for tc in suggestions)


# ---------------------------------------------------------------------------
# Format and Validation
# ---------------------------------------------------------------------------

class TestFormatAndValidation:
    """Test output format and input validation."""

    def test_output_format_dict(self, sample_project):
        ctx = run_test_focus(sample_project, output_format="dict")
        assert isinstance(ctx, dict)

    def test_output_format_markdown(self, sample_project):
        md = run_test_focus(sample_project, output_format="markdown",
                                changed_files=["src/mylib/transform.py"])
        assert isinstance(md, str)
        assert md.startswith("# Test Focus:")

    def test_invalid_format_raises(self, sample_project):
        with pytest.raises(ValueError, match="output_format"):
            run_test_focus(sample_project, output_format="xml")

    def test_invalid_root_raises(self):
        with pytest.raises(ValueError, match="existing directory"):
            run_test_focus("/nonexistent/path/123xyz")

    def test_no_changes_returns_gracefully(self, sample_project):
        """No changed_files or changed_functions should still work."""
        ctx = run_test_focus(sample_project)
        assert ctx["kind"] == "test_focus_context"
        assert ctx["metrics"]["changed_functions_count"] == 0


# ---------------------------------------------------------------------------
# Render Function
# ---------------------------------------------------------------------------

class TestRenderFunction:
    """Test render_test_focus_report."""

    def test_render_contains_summary(self, sample_project):
        ctx = run_test_focus(sample_project, changed_files=["src/mylib/transform.py"])
        md = run_render_report(ctx)
        assert "Test Focus:" in md
        assert ctx["summary"] in md

    def test_render_contains_metrics_table(self, sample_project):
        ctx = run_test_focus(sample_project, changed_files=["src/mylib/transform.py"])
        md = run_render_report(ctx)
        assert "| Metric | Value |" in md

    def test_render_contains_affected_files(self, sample_project):
        ctx = run_test_focus(sample_project, changed_files=["src/mylib/transform.py"])
        md = run_render_report(ctx)
        assert "test_transform.py" in md


# ---------------------------------------------------------------------------
# Helper Functions
# ---------------------------------------------------------------------------

class TestHelperFunctions:
    """Test internal helper functions."""

    def test_path_to_module_name_basic(self):
        result = _path_to_module_name("src/mylib/transform.py", "src")
        assert result == "mylib.transform"

    def test_path_to_module_name_init(self):
        result = _path_to_module_name("src/mylib/__init__.py", "src")
        assert result == "mylib"

    def test_path_to_module_name_nested(self):
        result = _path_to_module_name("src/mylib/sub/module.py", "src")
        assert result == "mylib.sub.module"

    def test_collect_test_files(self, sample_project):
        test_root = sample_project / "tests"
        files = _collect_test_files(test_root)
        names = [f.name for f in files]
        assert "test_transform.py" in names
        assert "test_utils.py" in names
        assert "__init__.py" not in names

    def test_collect_source_files(self, sample_project):
        src_root = sample_project / "src"
        files = _collect_source_files(src_root)
        names = [f.name for f in files]
        assert "transform.py" in names
        assert "__init__.py" in names
