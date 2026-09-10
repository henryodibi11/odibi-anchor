"""Tests for Phase 4 anchor("spec") lifecycle action (_spec.py)."""
from pathlib import Path

import pytest

from odibi_anchor._dispatcher._spec import (
    spec_action,
    _spec_list,
    _spec_create,
    _spec_status,
    _spec_done,
)
from odibi_anchor.codebase._spec_parser import list_specs, find_spec, parse_spec


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def specs_dir(tmp_path):
    """Create a tmp specs/ directory with a few test specs."""
    d = tmp_path / "specs"
    d.mkdir()
    # Spec with bold-text format
    (d / "ALPHA_SPEC.md").write_text(
        "# Alpha\n\n**Status:** ready  \n**Complexity:** low  \n\n"
        "## Success Criteria\n\n- Alpha works\n",
        encoding="utf-8",
    )
    # Spec with YAML frontmatter
    (d / "BETA_SPEC.md").write_text(
        "---\nstatus: done\ncomplexity: medium\nestimated_sessions: 1\n"
        "phases: []\nsuccess_criteria:\n  - Beta criterion\nfiles_touched: []\n---\n\n"
        "# Beta\n\n**Status:** \u2705 Done  \n",
        encoding="utf-8",
    )
    return d


@pytest.fixture
def root_with_specs(tmp_path, specs_dir):
    """A root dir with specs/ subdir."""
    return tmp_path


# ---------------------------------------------------------------------------
# spec_action -- list
# ---------------------------------------------------------------------------


class TestSpecActionList:
    """Tests for anchor("spec") list sub-command."""

    def test_lists_specs(self, root_with_specs, specs_dir):
        result = spec_action(
            root_with_specs, specs_dir=specs_dir, output_format="markdown"
        )
        assert "ALPHA" in result
        assert "BETA" in result
        assert "Specs (2 total)" in result

    def test_dict_format(self, root_with_specs, specs_dir):
        result = spec_action(
            root_with_specs, specs_dir=specs_dir, output_format="dict"
        )
        assert isinstance(result, dict)
        assert result["total"] == 2
        assert len(result["specs"]) == 2

    def test_empty_specs_dir(self, tmp_path):
        empty = tmp_path / "specs"
        empty.mkdir()
        result = spec_action(tmp_path, specs_dir=empty)
        assert "No specs found" in result


# ---------------------------------------------------------------------------
# spec_action -- create
# ---------------------------------------------------------------------------


class TestSpecActionCreate:
    """Tests for anchor("spec", "create", name="X")."""

    def test_creates_spec_file(self, tmp_path):
        specs_dir = tmp_path / "specs"
        result = spec_action(
            tmp_path, "create", name="MY_FEATURE",
            specs_dir=specs_dir, output_format="dict"
        )
        assert result["created"] is True
        spec_path = Path(result["path"])
        assert spec_path.exists()
        content = spec_path.read_text()
        assert "status: draft" in content
        assert "MY_FEATURE" in result["name"]

    def test_scaffolds_yaml_frontmatter(self, tmp_path):
        specs_dir = tmp_path / "specs"
        spec_action(
            tmp_path, "create", name="NEW_SPEC",
            specs_dir=specs_dir,
        )
        content = (specs_dir / "NEW_SPEC_SPEC.md").read_text()
        assert "---" in content
        assert "success_criteria: []" in content
        assert "## Success Criteria" in content

    def test_normalizes_name(self, tmp_path):
        specs_dir = tmp_path / "specs"
        result = spec_action(
            tmp_path, "create", name="my feature",
            specs_dir=specs_dir, output_format="dict"
        )
        assert result["name"] == "MY_FEATURE"

    def test_no_name_returns_error(self, tmp_path):
        result = spec_action(
            tmp_path, "create", output_format="dict"
        )
        assert "error" in result

    def test_duplicate_returns_error(self, tmp_path, specs_dir):
        result = spec_action(
            tmp_path, "create", name="ALPHA",
            specs_dir=specs_dir, output_format="dict"
        )
        assert "error" in result
        assert "already exists" in result["error"]


# ---------------------------------------------------------------------------
# spec_action -- status
# ---------------------------------------------------------------------------


class TestSpecActionStatus:
    """Tests for anchor("spec", "status", "X")."""

    def test_shows_spec_detail(self, root_with_specs, specs_dir):
        result = spec_action(
            root_with_specs, "status", "ALPHA",
            specs_dir=specs_dir, output_format="markdown"
        )
        assert "ALPHA" in result
        assert "ready" in result

    def test_dict_format_returns_spec_dict(self, root_with_specs, specs_dir):
        result = spec_action(
            root_with_specs, "status", "BETA",
            specs_dir=specs_dir, output_format="dict"
        )
        assert isinstance(result, dict)
        assert result["name"] == "BETA"
        assert result["status"] == "done"

    def test_not_found_returns_error(self, root_with_specs, specs_dir):
        result = spec_action(
            root_with_specs, "status", "NONEXISTENT",
            specs_dir=specs_dir, output_format="dict"
        )
        assert "error" in result

    def test_no_query_returns_usage(self, tmp_path):
        result = spec_action(tmp_path, "status")
        assert "Usage" in result


# ---------------------------------------------------------------------------
# spec_action -- done
# ---------------------------------------------------------------------------


class TestSpecActionDone:
    """Tests for anchor("spec", "done", "X")."""

    def test_marks_bold_text_done(self, root_with_specs, specs_dir):
        result = spec_action(
            root_with_specs, "done", "ALPHA",
            specs_dir=specs_dir, output_format="dict"
        )
        assert result["updated"] is True
        text = (specs_dir / "ALPHA_SPEC.md").read_text(encoding="utf-8")
        assert "✅ Done" in text

    def test_updates_yaml_frontmatter(self, root_with_specs, specs_dir):
        # BETA has YAML frontmatter with status: done -- re-mark it to verify edit
        # First create a fresh spec with status: in-progress
        (specs_dir / "GAMMA_SPEC.md").write_text(
            "---\nstatus: in-progress\ncomplexity: low\nestimated_sessions: 1\n"
            "phases: []\nsuccess_criteria: []\nfiles_touched: []\n---\n\n"
            "# Gamma\n\n**Status:** in-progress  \n",
            encoding="utf-8",
        )
        result = spec_action(
            root_with_specs, "done", "GAMMA",
            specs_dir=specs_dir, output_format="dict"
        )
        assert result["updated"] is True
        content = (specs_dir / "GAMMA_SPEC.md").read_text(encoding="utf-8")
        assert "status: done" in content
        assert "✅ Done" in content

    def test_not_found_returns_error(self, root_with_specs, specs_dir):
        result = spec_action(
            root_with_specs, "done", "NONEXISTENT",
            specs_dir=specs_dir, output_format="dict"
        )
        assert "error" in result

    def test_no_query_returns_error(self, tmp_path):
        result = spec_action(tmp_path, "done", output_format="dict")
        assert "error" in result


# ---------------------------------------------------------------------------
# spec_action -- unknown sub-command
# ---------------------------------------------------------------------------


class TestSpecActionUnknownCommand:
    def test_unknown_returns_error(self, tmp_path):
        result = spec_action(tmp_path, "badcmd", output_format="dict")
        assert "error" in result
        assert "badcmd" in result["error"]
