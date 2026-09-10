"""Tests for consistency_check_context."""

import os
import tempfile
from pathlib import Path

import pytest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from odibi_anchor.codebase import (
    consistency_check_context,
    render_consistency_check_report,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def clean_project(tmp_path):
    """A project that passes all rules."""
    src = tmp_path / "src" / "mylib"
    src.mkdir(parents=True)

    # __init__.py with proper exports
    (src / "__init__.py").write_text(
        "from mylib.tools import my_context, render_my_report\n\n"
        '__all__ = ["my_context", "render_my_report"]\n'
    )

    # Source file with proper conventions
    (src / "tools.py").write_text(
        'def my_context(root, *, subject=None, output_format="dict"):\n'
        '    """Generates context for testing."""\n'
        '    ctx = {\n'
        '        "kind": "my_context",\n'
        '        "subject": subject or "test",\n'
        '        "summary": "All good.",\n'
        '        "metrics": {"files": 1},\n'
        '        "findings": [],\n'
        '        "risks": [],\n'
        '        "samples": [],\n'
        '        "suggested_next_actions": [],\n'
        '    }\n'
        '    if output_format == "markdown":\n'
        '        return render_my_report(ctx)\n'
        '    return ctx\n'
        '\n\n'
        'def render_my_report(ctx):\n'
        '    """Renders my_context as markdown."""\n'
        '    return f"# {ctx[\'subject\']}"\n'
    )

    # Test file
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_tools.py").write_text(
        "def test_my_context():\n    pass\n"
    )

    return tmp_path


@pytest.fixture
def broken_project(tmp_path):
    """A project that violates multiple rules."""
    src = tmp_path / "src" / "badlib"
    src.mkdir(parents=True)

    # __init__.py with orphaned export
    (src / "__init__.py").write_text(
        "from badlib.tools import broken_context\n\n"
        '__all__ = ["broken_context", "ghost_function"]\n'
    )

    # Source: missing docstring, no render pair, no output_format, bad contract
    (src / "tools.py").write_text(
        "def broken_context(root):\n"
        "    ctx = {\n"
        '        "kind": "broken_context",\n'
        '        "subject": "test",\n'
        "    }\n"
        "    return ctx\n"
        "\n\n"
        "def BadlyNamed():\n"
        '    """This function has bad naming."""\n'
        "    pass\n"
    )

    return tmp_path


# ---------------------------------------------------------------------------
# TestOutputContract
# ---------------------------------------------------------------------------

class TestOutputContract:
    """Verify standard context dict contract."""

    def test_kind(self, clean_project):
        ctx = consistency_check_context(clean_project)
        assert ctx["kind"] == "consistency_check_context"

    def test_required_keys(self, clean_project):
        ctx = consistency_check_context(clean_project)
        required = {"kind", "subject", "summary", "metrics", "findings",
                    "risks", "samples", "suggested_next_actions"}
        assert required.issubset(ctx.keys())

    def test_subject_defaults_to_dirname(self, clean_project):
        ctx = consistency_check_context(clean_project)
        assert ctx["subject"] == clean_project.name

    def test_subject_override(self, clean_project):
        ctx = consistency_check_context(clean_project, subject="custom")
        assert ctx["subject"] == "custom"

    def test_metrics_structure(self, clean_project):
        ctx = consistency_check_context(clean_project)
        m = ctx["metrics"]
        assert "rules_checked" in m
        assert "violation_count" in m
        assert "compliant_count" in m
        assert "files_scanned" in m
        assert "violation_by_rule" in m


# ---------------------------------------------------------------------------
# TestCleanProject
# ---------------------------------------------------------------------------

class TestCleanProject:
    """Verify a well-structured project passes all rules."""

    def test_no_export_violations(self, clean_project):
        ctx = consistency_check_context(clean_project, rules=["export_completeness"])
        assert ctx["metrics"]["violation_count"] == 0

    def test_render_pairing_passes(self, clean_project):
        ctx = consistency_check_context(clean_project, rules=["render_pairing"])
        assert ctx["metrics"]["violation_count"] == 0

    def test_output_format_passes(self, clean_project):
        ctx = consistency_check_context(clean_project, rules=["output_format_param"])
        assert ctx["metrics"]["violation_count"] == 0

    def test_docstring_passes(self, clean_project):
        ctx = consistency_check_context(clean_project, rules=["docstring_coverage"])
        assert ctx["metrics"]["violation_count"] == 0

    def test_naming_passes(self, clean_project):
        ctx = consistency_check_context(clean_project, rules=["naming_conventions"])
        assert ctx["metrics"]["violation_count"] == 0

    def test_return_contract_passes(self, clean_project):
        ctx = consistency_check_context(clean_project, rules=["return_contract"])
        assert ctx["metrics"]["violation_count"] == 0


# ---------------------------------------------------------------------------
# TestBrokenProject
# ---------------------------------------------------------------------------

class TestBrokenProject:
    """Verify violations are detected."""

    def test_export_completeness_violation(self, broken_project):
        ctx = consistency_check_context(broken_project, rules=["export_completeness"])
        violations = [v for v in ctx["violations"] if v["rule"] == "export_completeness"]
        assert any("ghost_function" in v["detail"] for v in violations)

    def test_render_pairing_violation(self, broken_project):
        ctx = consistency_check_context(broken_project, rules=["render_pairing"])
        violations = [v for v in ctx["violations"] if v["rule"] == "render_pairing"]
        assert any("broken_context" in v["detail"] for v in violations)

    def test_output_format_violation(self, broken_project):
        ctx = consistency_check_context(broken_project, rules=["output_format_param"])
        violations = [v for v in ctx["violations"] if v["rule"] == "output_format_param"]
        assert any("broken_context" in v["detail"] for v in violations)

    def test_docstring_violation(self, broken_project):
        ctx = consistency_check_context(broken_project, rules=["docstring_coverage"])
        violations = [v for v in ctx["violations"] if v["rule"] == "docstring_coverage"]
        assert any("broken_context" in v["detail"] for v in violations)

    def test_naming_violation(self, broken_project):
        ctx = consistency_check_context(broken_project, rules=["naming_conventions"])
        violations = [v for v in ctx["violations"] if v["rule"] == "naming_conventions"]
        assert any("BadlyNamed" in v["detail"] for v in violations)

    def test_test_coverage_violation(self, broken_project):
        ctx = consistency_check_context(broken_project, rules=["test_coverage"])
        violations = [v for v in ctx["violations"] if v["rule"] == "test_coverage"]
        assert any("test_tools" in v["detail"] for v in violations)

    def test_return_contract_violation(self, broken_project):
        ctx = consistency_check_context(broken_project, rules=["return_contract"])
        violations = [v for v in ctx["violations"] if v["rule"] == "return_contract"]
        # broken_context is missing: summary, metrics, findings, risks
        assert any("broken_context" in v["detail"] for v in violations)
        assert any("summary" in v["detail"] for v in violations)

    def test_violation_has_severity(self, broken_project):
        ctx = consistency_check_context(broken_project)
        for v in ctx["violations"]:
            assert "severity" in v
            assert v["severity"] in {"blocker", "warning", "info"}


# ---------------------------------------------------------------------------
# TestRuleFiltering
# ---------------------------------------------------------------------------

class TestRuleFiltering:
    """Verify rule selection works."""

    def test_run_single_rule(self, broken_project):
        ctx = consistency_check_context(broken_project, rules=["naming_conventions"])
        assert ctx["metrics"]["rules_checked"] == 1
        assert all(v["rule"] == "naming_conventions" for v in ctx["violations"])

    def test_exclude_rule(self, broken_project):
        ctx = consistency_check_context(broken_project, exclude_rules=["naming_conventions"])
        assert not any(v["rule"] == "naming_conventions" for v in ctx["violations"])

    def test_exclude_paths(self, broken_project):
        # Create file in excluded path
        excluded = broken_project / "vendor"
        excluded.mkdir()
        (excluded / "bad.py").write_text("def BadName():\n    pass\n")

        ctx = consistency_check_context(
            broken_project,
            rules=["naming_conventions"],
            exclude_paths=["vendor/"],
        )
        assert not any("vendor" in v["file"] for v in ctx["violations"])

    def test_all_rules_run_by_default(self, broken_project):
        ctx = consistency_check_context(broken_project)
        assert ctx["metrics"]["rules_checked"] == 7


# ---------------------------------------------------------------------------
# TestOutputFormat
# ---------------------------------------------------------------------------

class TestOutputFormat:
    """Verify output_format dispatch."""

    def test_dict_returns_dict(self, clean_project):
        result = consistency_check_context(clean_project, output_format="dict")
        assert isinstance(result, dict)

    def test_markdown_returns_string(self, broken_project):
        result = consistency_check_context(broken_project, output_format="markdown")
        assert isinstance(result, str)
        assert "Consistency Check" in result

    def test_invalid_format_raises(self, clean_project):
        with pytest.raises(ValueError, match="output_format"):
            consistency_check_context(clean_project, output_format="xml")

    def test_invalid_root_raises(self):
        with pytest.raises(ValueError, match="existing directory"):
            consistency_check_context("/nonexistent/path/xyz")


# ---------------------------------------------------------------------------
# TestRender
# ---------------------------------------------------------------------------

class TestRender:
    """Verify markdown renderer."""

    def test_pass_report(self, clean_project):
        ctx = consistency_check_context(clean_project)
        md = render_consistency_check_report(ctx)
        assert "PASS" in md

    def test_violations_report(self, broken_project):
        ctx = consistency_check_context(broken_project)
        md = render_consistency_check_report(ctx)
        assert "VIOLATIONS FOUND" in md
        assert "## Violations" in md
        assert "Suggested Actions" in md

    def test_render_missing_keys_raises(self):
        with pytest.raises(ValueError, match="Missing required keys"):
            render_consistency_check_report({"kind": "consistency_check_context"})

    def test_render_grouped_by_rule(self, broken_project):
        ctx = consistency_check_context(broken_project)
        md = render_consistency_check_report(ctx)
        # Should have rule name as heading
        assert "### " in md


# ---------------------------------------------------------------------------
# TestEdgeCases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    """Edge case handling."""

    def test_empty_directory(self, tmp_path):
        ctx = consistency_check_context(tmp_path)
        assert ctx["metrics"]["files_scanned"] == 0
        assert ctx["metrics"]["violation_count"] == 0

    def test_syntax_error_file_skipped(self, tmp_path):
        (tmp_path / "bad.py").write_text("def broken(\n  invalid syntax here\n")
        ctx = consistency_check_context(tmp_path)
        # Should not crash, file is skipped
        assert ctx["kind"] == "consistency_check_context"

    def test_hidden_dirs_excluded(self, tmp_path):
        hidden = tmp_path / ".hidden"
        hidden.mkdir()
        (hidden / "secret.py").write_text("def BadName():\n    pass\n")
        ctx = consistency_check_context(tmp_path, rules=["naming_conventions"])
        assert not any(".hidden" in v["file"] for v in ctx["violations"])

    def test_pycache_excluded(self, tmp_path):
        cache = tmp_path / "__pycache__"
        cache.mkdir()
        (cache / "mod.cpython-311.py").write_text("x = 1\n")
        ctx = consistency_check_context(tmp_path)
        assert ctx["metrics"]["files_scanned"] == 0


# ---------------------------------------------------------------------------
# TestDogFood
# ---------------------------------------------------------------------------

class TestDogFood:
    """Run against the real odibi_anchor project."""

    ROOT = str(Path(__file__).resolve().parent.parent)

    def test_real_project_runs(self):
        ctx = consistency_check_context(
            self.ROOT,
            exclude_paths=["drafts/", "scripts/", "examples/"],
        )
        assert ctx["kind"] == "consistency_check_context"
        assert ctx["metrics"]["files_scanned"] > 20

    def test_real_project_return_contract(self):
        ctx = consistency_check_context(
            self.ROOT,
            rules=["return_contract"],
            exclude_paths=["drafts/", "scripts/", "examples/", "tests/"],
        )
        # All context generators should have return contract
        compliant = [c for c in ctx["compliant"] if c["rule"] == "return_contract"]
        assert len(compliant) >= 5

    def test_real_project_render_pairing(self):
        ctx = consistency_check_context(
            self.ROOT,
            rules=["render_pairing"],
            exclude_paths=["drafts/", "scripts/", "examples/", "tests/"],
        )
        # Most tools have render pairs
        compliant = [c for c in ctx["compliant"] if c["rule"] == "render_pairing"]
        assert len(compliant) >= 7
