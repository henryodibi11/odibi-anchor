"""Tests for spec execute sub-command (Phase 5)."""

import json
import pytest
from pathlib import Path
from datetime import datetime, timezone

from odibi_anchor._dispatcher._spec import (
    spec_action,
    _spec_execute,
    _update_spec_status,
)


@pytest.fixture
def exec_project(tmp_path):
    """Create a project with valid spec for execute testing."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "src").mkdir()
    (project / "src" / "main.py").write_text("import os\nprint('hello')\n")
    (project / "tests").mkdir()
    (project / "tests" / "test_main.py").write_text("def test_pass(): assert True\n")

    specs_dir = project / "specs"
    specs_dir.mkdir()

    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    spec_content = (
        "---\n"
        "status: ready\n"
        "mode: implementation\n"
        f"created: {now_iso}\n"
        "phases: []\n"
        "success_criteria:\n"
        "  - Tests pass\n"
        "  - Code is clean\n"
        "files_touched:\n"
        "  - src/main.py\n"
        "  - tests/test_main.py\n"
        "dependencies: []\n"
        "permissions_needed: []\n"
        "---\n\n"
        "**Status:** Ready\n\n"
        "# Test Feature\n"
    )
    (specs_dir / "TEST_FEATURE_SPEC.md").write_text(spec_content)

    return project, specs_dir


class TestSpecExecute:
    """Tests for _spec_execute via spec_action."""

    def test_executes_valid_spec(self, exec_project):
        project, specs_dir = exec_project
        result = spec_action(
            str(project), "execute", "TEST_FEATURE",
            specs_dir=str(specs_dir), output_format="dict"
        )
        assert result["executing"] is True
        assert result["spec_name"] == "TEST_FEATURE"

    def test_returns_phases(self, exec_project):
        project, specs_dir = exec_project
        result = spec_action(
            str(project), "execute", "TEST_FEATURE",
            specs_dir=str(specs_dir), output_format="dict"
        )
        assert result["total_phases"] >= 1
        assert len(result["phases"]) >= 1

    def test_returns_acceptance_criteria(self, exec_project):
        project, specs_dir = exec_project
        result = spec_action(
            str(project), "execute", "TEST_FEATURE",
            specs_dir=str(specs_dir), output_format="dict"
        )
        assert "Tests pass" in result["acceptance_criteria"]
        assert "Code is clean" in result["acceptance_criteria"]

    def test_returns_active_spec(self, exec_project):
        project, specs_dir = exec_project
        result = spec_action(
            str(project), "execute", "TEST_FEATURE",
            specs_dir=str(specs_dir), output_format="dict"
        )
        active = result["active_spec"]
        assert active["name"] == "TEST_FEATURE"
        assert active["status"] == "executing"
        assert "success_criteria" in active
        assert "files_touched" in active

    def test_updates_spec_status(self, exec_project):
        project, specs_dir = exec_project
        spec_action(
            str(project), "execute", "TEST_FEATURE",
            specs_dir=str(specs_dir), output_format="dict"
        )
        spec_text = (specs_dir / "TEST_FEATURE_SPEC.md").read_text()
        assert "status: executing" in spec_text

    def test_updates_bold_status_line(self, exec_project):
        project, specs_dir = exec_project
        spec_action(
            str(project), "execute", "TEST_FEATURE",
            specs_dir=str(specs_dir), output_format="dict"
        )
        spec_text = (specs_dir / "TEST_FEATURE_SPEC.md").read_text()
        assert "Executing" in spec_text

    def test_blocks_done_spec(self, exec_project):
        project, specs_dir = exec_project
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        (specs_dir / "DONE_SPEC.md").write_text(
            f"---\nstatus: done\nfiles_touched: []\ndependencies: []\ncreated: {now_iso}\n---\n# Done\n"
        )
        result = spec_action(
            str(project), "execute", "DONE",
            specs_dir=str(specs_dir), output_format="dict"
        )
        assert "error" in result

    def test_blocks_stale_spec(self, exec_project):
        project, specs_dir = exec_project
        # Delete a tracked file to make spec stale
        (project / "src" / "main.py").unlink()
        result = spec_action(
            str(project), "execute", "TEST_FEATURE",
            specs_dir=str(specs_dir), output_format="dict"
        )
        assert "error" in result
        assert "validation" in result

    def test_missing_spec_error(self, exec_project):
        project, specs_dir = exec_project
        result = spec_action(
            str(project), "execute", "NONEXISTENT",
            specs_dir=str(specs_dir), output_format="dict"
        )
        assert "error" in result

    def test_no_query_returns_usage(self, exec_project):
        project, specs_dir = exec_project
        result = spec_action(
            str(project), "execute", None,
            specs_dir=str(specs_dir), output_format="dict"
        )
        assert "error" in result

    def test_markdown_output(self, exec_project):
        project, specs_dir = exec_project
        result = spec_action(
            str(project), "execute", "TEST_FEATURE",
            specs_dir=str(specs_dir), output_format="markdown"
        )
        assert "Executing:" in result
        assert "TEST_FEATURE" in result

    def test_suggested_next_actions(self, exec_project):
        project, specs_dir = exec_project
        result = spec_action(
            str(project), "execute", "TEST_FEATURE",
            specs_dir=str(specs_dir), output_format="dict"
        )
        assert len(result["suggested_next_actions"]) > 0

    def test_validation_summary(self, exec_project):
        project, specs_dir = exec_project
        result = spec_action(
            str(project), "execute", "TEST_FEATURE",
            specs_dir=str(specs_dir), output_format="dict"
        )
        assert "checks passed" in result["validation_summary"]


class TestUpdateSpecStatus:
    """Tests for _update_spec_status helper."""

    def test_updates_frontmatter_status(self, tmp_path):
        spec = tmp_path / "TEST_SPEC.md"
        spec.write_text("---\nstatus: ready\n---\n# Test\n")
        _update_spec_status(spec, "executing")
        assert "status: executing" in spec.read_text()

    def test_updates_bold_text_status(self, tmp_path):
        spec = tmp_path / "TEST_SPEC.md"
        spec.write_text("---\nstatus: ready\n---\n\n**Status:** Ready\n\n# Test\n")
        _update_spec_status(spec, "in-progress")
        text = spec.read_text()
        assert "In Progress" in text or "In-Progress" in text

    def test_preserves_other_content(self, tmp_path):
        spec = tmp_path / "TEST_SPEC.md"
        spec.write_text("---\nstatus: draft\ncomplexity: high\n---\n\n# Test\n\nBody content here.\n")
        _update_spec_status(spec, "executing")
        text = spec.read_text()
        assert "complexity: high" in text
        assert "Body content here." in text
