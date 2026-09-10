"""Tests for spec validate sub-command (Phase 4)."""

import json
import pytest
from pathlib import Path
from datetime import datetime, timezone, timedelta

from odibi_anchor._dispatcher._spec import (
    spec_action,
    _spec_validate,
    _compute_file_hash,
    _can_resolve_import,
)


@pytest.fixture
def validate_project(tmp_path):
    """Create a project with spec for validation testing."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "src").mkdir()
    (project / "src" / "main.py").write_text("import os\nimport json\nprint('hello')\n")
    (project / "tests").mkdir()
    (project / "tests" / "test_main.py").write_text("def test_pass(): assert True\n")

    # Specs dir
    specs_dir = project / "specs"
    specs_dir.mkdir()

    # Fresh spec (created today)
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    spec_content = (
        "---\n"
        "status: draft\n"
        "mode: implementation\n"
        f"created: {now_iso}\n"
        "files_touched:\n"
        '  - "src/main.py"\n'
        '  - "tests/test_main.py"\n'
        "dependencies:\n"
        '  - "pytest>=7.0"\n'
        "permissions_needed: []\n"
        "---\n\n"
        "# Test Feature\n"
    )
    (specs_dir / "TEST_FEATURE_SPEC.md").write_text(spec_content)

    return project, specs_dir


class TestComputeFileHash:
    """Tests for _compute_file_hash."""

    def test_consistent(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("hello")
        assert _compute_file_hash(f) == _compute_file_hash(f)
        assert len(_compute_file_hash(f)) == 64

    def test_different_content(self, tmp_path):
        f1 = tmp_path / "a.txt"
        f2 = tmp_path / "b.txt"
        f1.write_text("hello")
        f2.write_text("world")
        assert _compute_file_hash(f1) != _compute_file_hash(f2)


class TestCanResolveImport:
    """Tests for _can_resolve_import."""

    def test_stdlib_resolves(self):
        assert _can_resolve_import("os") is True
        assert _can_resolve_import("json") is True

    def test_nonexistent_fails(self):
        assert _can_resolve_import("nonexistent_xyz_abc") is False

    def test_dotted_checks_top_level(self):
        assert _can_resolve_import("os.path") is True


class TestSpecValidate:
    """Tests for _spec_validate via spec_action."""

    def test_all_pass_when_fresh(self, validate_project):
        project, specs_dir = validate_project
        result = spec_action(
            str(project), "validate", "TEST_FEATURE",
            specs_dir=str(specs_dir), output_format="dict"
        )
        assert result["valid"] is True
        assert len(result["checks"]) == 5
        assert all(c["passed"] for c in result["checks"])

    def test_detects_missing_file(self, validate_project):
        project, specs_dir = validate_project
        (project / "tests" / "test_main.py").unlink()
        result = spec_action(
            str(project), "validate", "TEST_FEATURE",
            specs_dir=str(specs_dir), output_format="dict"
        )
        assert result["valid"] is False
        assert result["has_errors"] is True
        files_check = next(c for c in result["checks"] if c["name"] == "files_exist")
        assert files_check["passed"] is False
        assert "tests/test_main.py" in files_check["missing"]

    def test_detects_stale_file(self, validate_project):
        project, specs_dir = validate_project
        import time; time.sleep(0.05)  # ensure mtime is detectably after spec
        (project / "src" / "main.py").write_text("# totally different\n")
        result = spec_action(
            str(project), "validate", "TEST_FEATURE",
            specs_dir=str(specs_dir), output_format="dict"
        )
        assert result["valid"] is False
        assert len(result["stale_files"]) == 1
        assert result["stale_files"][0]["file"] == "src/main.py"

    def test_detects_missing_dependency(self, validate_project):
        project, specs_dir = validate_project
        # Override spec with nonexistent dependency
        spec_content = (
            "---\n"
            "status: draft\n"
            "created: 2026-06-06T12:00:00Z\n"
            "files_touched: []\n"
            "dependencies:\n"
            '  - "totally_fake_package>=99.0"\n'
            "---\n\n# Test\n"
        )
        (specs_dir / "TEST_FEATURE_SPEC.md").write_text(spec_content)
        result = spec_action(
            str(project), "validate", "TEST_FEATURE",
            specs_dir=str(specs_dir), output_format="dict"
        )
        deps_check = next(c for c in result["checks"] if c["name"] == "dependencies_available")
        assert deps_check["passed"] is False
        assert len(result["missing_deps"]) == 1

    def test_detects_old_spec(self, validate_project):
        project, specs_dir = validate_project
        # Make spec 30 days old
        old_date = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        spec_content = (
            "---\n"
            "status: draft\n"
            f"created: {old_date}\n"
            "files_touched: []\n"
            "dependencies: []\n"
            "---\n\n# Old Spec\n"
        )
        (specs_dir / "TEST_FEATURE_SPEC.md").write_text(spec_content)
        result = spec_action(
            str(project), "validate", "TEST_FEATURE",
            specs_dir=str(specs_dir), output_format="dict"
        )
        age_check = next(c for c in result["checks"] if c["name"] == "spec_age")
        assert age_check["passed"] is False
        assert age_check["age_days"] >= 30

    def test_suggested_actions_present(self, validate_project):
        project, specs_dir = validate_project
        (project / "src" / "main.py").write_text("# changed\n")
        result = spec_action(
            str(project), "validate", "TEST_FEATURE",
            specs_dir=str(specs_dir), output_format="dict"
        )
        assert len(result["suggested_next_actions"]) > 0

    def test_missing_spec_returns_error(self, validate_project):
        project, specs_dir = validate_project
        result = spec_action(
            str(project), "validate", "NONEXISTENT",
            specs_dir=str(specs_dir), output_format="dict"
        )
        assert "error" in result

    def test_no_query_returns_usage(self, validate_project):
        project, specs_dir = validate_project
        result = spec_action(
            str(project), "validate", None,
            specs_dir=str(specs_dir), output_format="dict"
        )
        assert "error" in result

    def test_markdown_output(self, validate_project):
        project, specs_dir = validate_project
        result = spec_action(
            str(project), "validate", "TEST_FEATURE",
            specs_dir=str(specs_dir), output_format="markdown"
        )
        assert "Spec Validation" in result
        assert "files_exist" in result
