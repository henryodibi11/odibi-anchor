"""Tests for spec review sub-command (Phase 6)."""

import json
import pytest
from pathlib import Path
from datetime import datetime, timezone

from odibi_anchor._dispatcher._spec import (
    spec_action,
    _spec_review,
    _has_docstring,
)


@pytest.fixture
def review_project(tmp_path, monkeypatch):
    """Create a project with spec for review testing."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "src").mkdir()
    (project / "src" / "main.py").write_text('\"\"\"Main module.\"\"\"\nimport os\nprint("hello")\n')
    (project / "tests").mkdir()
    (project / "tests" / "test_main.py").write_text("def test_works(): assert True\n")

    specs_dir = project / "specs"
    specs_dir.mkdir()

    central_skills = tmp_path / "central-skills"
    for name in ("planning-implementation", "writing-specs", "code-standards"):
        skill_dir = central_skills / name
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(f"# {name}\n", encoding="utf-8")
    from odibi_anchor._dispatcher._boot import _ENV
    monkeypatch.setitem(_ENV, "skills_dir", str(central_skills))

    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    # Good spec with all sections
    spec_content = (
        "---\n"
        "status: ready\n"
        "mode: implementation\n"
        f"created: {now_iso}\n"
        "files_touched:\n"
        "  - src/main.py\n"
        "permissions_needed: []\n"
        "dependencies: []\n"
        "---\n\n"
        "# Feature\n\n"
        "## Problem\n\nWe need this.\n\n"
        "## Design\n\nDo it.\n\n"
        "Use anchor(\"safe\") to apply changes.\n\n"
        "Follow planning-implementation, writing-specs, and code-standards.\n\n"
        "## Acceptance Criteria\n\n- WHEN tests run, the system SHALL pass all assertions\n\n"
        "## Verification\n\nRun tests.\n\n"
        "## Risks\n\nMight break things.\n"
    )
    (specs_dir / "GOOD_FEATURE_SPEC.md").write_text(spec_content)

    return project, specs_dir


class TestHasDocstring:
    """Tests for _has_docstring helper."""

    def test_detects_double_triple_quotes(self):
        assert _has_docstring('\"\"\"docstring\"\"\"') is True

    def test_detects_single_triple_quotes(self):
        assert _has_docstring("\'\'\'\'docstring\'\'\'") is True

    def test_no_docstring(self):
        assert _has_docstring("import os\nprint('hi')\n") is False


class TestSpecReview:
    """Tests for _spec_review via spec_action."""

    def test_good_spec_passes_all(self, review_project):
        project, specs_dir = review_project
        result = spec_action(
            str(project), "review", "GOOD_FEATURE",
            specs_dir=str(specs_dir), output_format="dict"
        )
        assert result["all_passed"] is True
        assert result["rating"] == "excellent"
        assert result["passed"] == 8

    def test_returns_8_checks(self, review_project):
        project, specs_dir = review_project
        result = spec_action(
            str(project), "review", "GOOD_FEATURE",
            specs_dir=str(specs_dir), output_format="dict"
        )
        assert result["total"] == 8
        names = [c["name"] for c in result["checks"]]
        assert "completeness" in names
        assert "standards_alignment" in names
        assert "test_coverage" in names
        assert "tool_usage" in names
        assert "permission_declaration" in names
        assert "scope_sanity" in names
        assert "risk_assessment" in names
        assert "standards_cross_ref" in names

    def test_detects_missing_sections(self, review_project):
        project, specs_dir = review_project
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        (specs_dir / "BARE_SPEC.md").write_text(
            f"---\nstatus: ready\nmode: analysis\ncreated: {now_iso}\n"
            f"files_touched: []\ndependencies: []\n---\n\n# Bare\n\nNo sections.\n"
        )
        result = spec_action(
            str(project), "review", "BARE",
            specs_dir=str(specs_dir), output_format="dict"
        )
        comp = next(c for c in result["checks"] if c["name"] == "completeness")
        assert comp["passed"] is False
        assert len(comp["missing"]) > 0

    def test_detects_missing_risks_for_impl(self, review_project):
        project, specs_dir = review_project
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        (specs_dir / "NORISK_SPEC.md").write_text(
            f"---\nstatus: ready\nmode: implementation\ncreated: {now_iso}\n"
            f"files_touched: []\ndependencies: []\n---\n\n# No Risk\n\n"
            f"## Problem\n\nP.\n\n## Design\n\nD.\n\n"
            f"## Acceptance Criteria\n\n- Done\n\n## Verification\n\nV.\n"
        )
        result = spec_action(
            str(project), "review", "NORISK",
            specs_dir=str(specs_dir), output_format="dict"
        )
        risk_check = next(c for c in result["checks"] if c["name"] == "risk_assessment")
        assert risk_check["passed"] is False

    def test_risk_not_required_for_analysis(self, review_project):
        project, specs_dir = review_project
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        (specs_dir / "ANALYSIS_SPEC.md").write_text(
            f"---\nstatus: ready\nmode: analysis\ncreated: {now_iso}\n"
            f"files_touched: []\ndependencies: []\n---\n\n# Analysis\n\n"
            f"## Problem\n\nP.\n\n## Design\n\nD.\n\n"
            f"## Acceptance Criteria\n\n- Done\n\n## Verification\n\nV.\n"
        )
        result = spec_action(
            str(project), "review", "ANALYSIS",
            specs_dir=str(specs_dir), output_format="dict"
        )
        risk_check = next(c for c in result["checks"] if c["name"] == "risk_assessment")
        assert risk_check["passed"] is True

    def test_detects_large_scope(self, review_project):
        project, specs_dir = review_project
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        files_list = "\n".join([f"  - src/f{i}.py" for i in range(12)])
        (specs_dir / "BIG_SPEC.md").write_text(
            f"---\nstatus: ready\nmode: analysis\ncreated: {now_iso}\n"
            f"files_touched:\n{files_list}\ndependencies: []\n---\n\n# Big\n\n"
            f"## Problem\n\nP.\n\n## Design\n\nD.\n\n"
            f"## Acceptance Criteria\n\n- Done\n\n## Verification\n\nV.\n"
        )
        result = spec_action(
            str(project), "review", "BIG",
            specs_dir=str(specs_dir), output_format="dict"
        )
        scope_check = next(c for c in result["checks"] if c["name"] == "scope_sanity")
        assert scope_check["passed"] is False

    def test_detects_missing_tool_refs(self, review_project):
        project, specs_dir = review_project
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        (specs_dir / "NOTOOL_SPEC.md").write_text(
            f"---\nstatus: ready\nmode: analysis\ncreated: {now_iso}\n"
            f"files_touched: []\ndependencies: []\npermissions_needed: []\n---\n\n"
            f"# No Tools\n\n## Problem\n\nP.\n\n## Design\n\nD.\n\n"
            f"## Acceptance Criteria\n\n- Done\n\n## Verification\n\nV.\n"
        )
        result = spec_action(
            str(project), "review", "NOTOOL",
            specs_dir=str(specs_dir), output_format="dict"
        )
        tool_check = next(c for c in result["checks"] if c["name"] == "tool_usage")
        assert tool_check["passed"] is False

    def test_persists_review_section(self, review_project):
        project, specs_dir = review_project
        spec_action(
            str(project), "review", "GOOD_FEATURE",
            specs_dir=str(specs_dir), output_format="dict"
        )
        text = (specs_dir / "GOOD_FEATURE_SPEC.md").read_text()
        assert "## Review" in text
        assert "Rating:" in text

    def test_replaces_existing_review(self, review_project):
        project, specs_dir = review_project
        # Run twice
        spec_action(
            str(project), "review", "GOOD_FEATURE",
            specs_dir=str(specs_dir), output_format="dict"
        )
        spec_action(
            str(project), "review", "GOOD_FEATURE",
            specs_dir=str(specs_dir), output_format="dict"
        )
        text = (specs_dir / "GOOD_FEATURE_SPEC.md").read_text()
        assert text.count("## Review") == 1

    def test_missing_spec_returns_error(self, review_project):
        project, specs_dir = review_project
        result = spec_action(
            str(project), "review", "NONEXISTENT",
            specs_dir=str(specs_dir), output_format="dict"
        )
        assert "error" in result

    def test_no_query_returns_usage(self, review_project):
        project, specs_dir = review_project
        result = spec_action(
            str(project), "review", None,
            specs_dir=str(specs_dir), output_format="dict"
        )
        assert "error" in result

    def test_markdown_output(self, review_project):
        project, specs_dir = review_project
        result = spec_action(
            str(project), "review", "GOOD_FEATURE",
            specs_dir=str(specs_dir), output_format="markdown"
        )
        # _spec_review always returns a dict (so post-dispatch can read "rating")
        # The markdown text is in result["summary"]
        assert isinstance(result, dict)
        assert "rating" in result
        assert "Spec Review:" in result["summary"]
        assert "GOOD_FEATURE" in result["summary"]

    def test_suggested_improvements_for_failures(self, review_project):
        project, specs_dir = review_project
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        (specs_dir / "POOR_SPEC.md").write_text(
            f"---\nstatus: ready\nmode: implementation\ncreated: {now_iso}\n"
            f"files_touched: []\ndependencies: []\n---\n\n# Poor\n"
        )
        result = spec_action(
            str(project), "review", "POOR",
            specs_dir=str(specs_dir), output_format="dict"
        )
        assert len(result["suggested_improvements"]) > 0
        assert result["rating"] in ("poor", "needs-work")

    def test_review_result_structure(self, review_project):
        project, specs_dir = review_project
        result = spec_action(
            str(project), "review", "GOOD_FEATURE",
            specs_dir=str(specs_dir), output_format="dict"
        )
        assert "spec_name" in result
        assert "rating" in result
        assert "passed" in result
        assert "total" in result
        assert "all_passed" in result
        assert "has_errors" in result
        assert "checks" in result
        assert "suggested_improvements" in result
        assert "review_persisted" in result
