"""Tests for change_impact_context — dog-food on odibi_anchor itself."""

import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from odibi_anchor.codebase import change_impact_context, render_change_impact_report


PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ===========================================================================
# Output Contract
# ===========================================================================

class TestOutputContract:
    """Verify standard context contract."""

    def test_kind(self):
        ctx = change_impact_context(
            root=PROJECT_ROOT,
            target="src/odibi_anchor/debugging/error_trace_context.py",
            function="error_trace_context",
        )
        assert ctx["kind"] == "change_impact_context"

    def test_has_required_keys(self):
        ctx = change_impact_context(
            root=PROJECT_ROOT,
            target="src/odibi_anchor/debugging/error_trace_context.py",
        )
        required = {"kind", "subject", "summary", "metrics", "findings", "risks",
                    "suggested_next_actions", "samples", "risk_assessment",
                    "importers", "call_sites", "test_files", "doc_references", "checklist"}
        assert required.issubset(ctx.keys())

    def test_summary_is_string(self):
        ctx = change_impact_context(
            root=PROJECT_ROOT,
            target="src/odibi_anchor/debugging/error_trace_context.py",
            function="error_trace_context",
        )
        assert isinstance(ctx["summary"], str)
        assert "error_trace_context" in ctx["summary"]


# ===========================================================================
# Add Parameter (backward-compatible)
# ===========================================================================

class TestAddParameter:
    """Test add_parameter change type — LOW risk."""

    def test_low_risk(self):
        ctx = change_impact_context(
            root=PROJECT_ROOT,
            target="src/odibi_anchor/debugging/error_trace_context.py",
            function="error_trace_context",
            change_type="add_parameter",
        )
        assert ctx["metrics"]["risk_level"] == "low"
        assert ctx["metrics"]["is_breaking"] is False

    def test_finds_importers(self):
        ctx = change_impact_context(
            root=PROJECT_ROOT,
            target="src/odibi_anchor/debugging/error_trace_context.py",
            function="error_trace_context",
            change_type="add_parameter",
        )
        assert ctx["metrics"]["importer_count"] >= 1
        # Should find __init__.py re-export
        init_importers = [i for i in ctx["importers"] if i["usage"] == "re-export"]
        assert len(init_importers) >= 1

    def test_finds_call_sites(self):
        ctx = change_impact_context(
            root=PROJECT_ROOT,
            target="src/odibi_anchor/debugging/error_trace_context.py",
            function="error_trace_context",
            change_type="add_parameter",
        )
        assert ctx["metrics"]["call_site_count"] > 0

    def test_finds_tests(self):
        ctx = change_impact_context(
            root=PROJECT_ROOT,
            target="src/odibi_anchor/debugging/error_trace_context.py",
            function="error_trace_context",
            change_type="add_parameter",
        )
        assert ctx["metrics"]["test_file_count"] >= 2

    def test_finds_docs(self):
        ctx = change_impact_context(
            root=PROJECT_ROOT,
            target="src/odibi_anchor/debugging/error_trace_context.py",
            function="error_trace_context",
            change_type="add_parameter",
        )
        assert ctx["metrics"]["doc_reference_count"] >= 1
        doc_files = [d["file"] for d in ctx["doc_references"]]
        assert any("error_trace_context" in f for f in doc_files)


# ===========================================================================
# Rename Function (BREAKING)
# ===========================================================================

class TestRenameFunction:
    """Test rename_function change type — HIGH risk."""

    def test_high_risk(self):
        ctx = change_impact_context(
            root=PROJECT_ROOT,
            target="src/odibi_anchor/validation/quality_gate_context.py",
            function="quality_gate_context",
            change_type="rename_function",
        )
        assert ctx["metrics"]["risk_level"] == "high"
        assert ctx["metrics"]["is_breaking"] is True

    def test_checklist_includes_imports_and_calls(self):
        ctx = change_impact_context(
            root=PROJECT_ROOT,
            target="src/odibi_anchor/validation/quality_gate_context.py",
            function="quality_gate_context",
            change_type="rename_function",
        )
        assert len(ctx["checklist"]) > 5
        assert any("Update import" in item for item in ctx["checklist"])
        assert any("Update call" in item for item in ctx["checklist"])

    def test_risks_mention_breaking(self):
        ctx = change_impact_context(
            root=PROJECT_ROOT,
            target="src/odibi_anchor/validation/quality_gate_context.py",
            function="quality_gate_context",
            change_type="rename_function",
        )
        assert any("BREAKING" in r for r in ctx["risks"])


# ===========================================================================
# Delete Function (BREAKING)
# ===========================================================================

class TestDeleteFunction:
    """Test delete_function change type."""

    def test_high_risk(self):
        ctx = change_impact_context(
            root=PROJECT_ROOT,
            target="src/odibi_anchor/validation/duplicate_key_context.py",
            function="duplicate_key_context",
            change_type="delete_function",
        )
        assert ctx["metrics"]["is_breaking"] is True


# ===========================================================================
# File-Level (no function specified)
# ===========================================================================

class TestFileLevel:
    """Test analysis without specifying a function."""

    def test_file_level_works(self):
        ctx = change_impact_context(
            root=PROJECT_ROOT,
            target="src/odibi_anchor/debugging/error_trace_context.py",
            change_type="refactor",
        )
        assert ctx["kind"] == "change_impact_context"
        assert ctx["metrics"]["function"] is None

    def test_finds_all_importers_of_module(self):
        ctx = change_impact_context(
            root=PROJECT_ROOT,
            target="src/odibi_anchor/_utils/engine_utils.py",
            change_type="modify_logic",
        )
        # engine_utils is imported by many validation/tables modules
        assert ctx["metrics"]["importer_count"] >= 3


# ===========================================================================
# Render Function
# ===========================================================================

class TestRender:
    """Test markdown rendering."""

    def test_returns_string(self):
        ctx = change_impact_context(
            root=PROJECT_ROOT,
            target="src/odibi_anchor/debugging/error_trace_context.py",
            function="error_trace_context",
        )
        result = render_change_impact_report(ctx)
        assert isinstance(result, str)

    def test_contains_header(self):
        ctx = change_impact_context(
            root=PROJECT_ROOT,
            target="src/odibi_anchor/debugging/error_trace_context.py",
            function="error_trace_context",
        )
        result = render_change_impact_report(ctx)
        assert "# Change Impact:" in result

    def test_contains_checklist(self):
        ctx = change_impact_context(
            root=PROJECT_ROOT,
            target="src/odibi_anchor/validation/quality_gate_context.py",
            function="quality_gate_context",
            change_type="rename_function",
        )
        result = render_change_impact_report(ctx)
        assert "## Update Checklist" in result
        assert "- [ ]" in result

    def test_missing_keys_raises(self):
        with pytest.raises(ValueError, match="Missing required keys"):
            render_change_impact_report({"kind": "change_impact_context"})


# ===========================================================================
# output_format Dispatch
# ===========================================================================

class TestOutputFormat:
    """Test output_format parameter."""

    def test_dict_default(self):
        ctx = change_impact_context(
            root=PROJECT_ROOT,
            target="src/odibi_anchor/debugging/error_trace_context.py",
        )
        assert isinstance(ctx, dict)

    def test_markdown(self):
        result = change_impact_context(
            root=PROJECT_ROOT,
            target="src/odibi_anchor/debugging/error_trace_context.py",
            function="error_trace_context",
            output_format="markdown",
        )
        assert isinstance(result, str)
        assert "# Change Impact:" in result

    def test_invalid_raises(self):
        with pytest.raises(ValueError, match="output_format"):
            change_impact_context(
                root=PROJECT_ROOT,
                target="src/odibi_anchor/debugging/error_trace_context.py",
                output_format="xml",
            )


# ===========================================================================
# Edge Cases
# ===========================================================================

class TestEdgeCases:
    """Edge case handling."""

    def test_nonexistent_target_raises(self):
        with pytest.raises(ValueError, match="not found"):
            change_impact_context(root=PROJECT_ROOT, target="nope.py")

    def test_nonexistent_root_raises(self, tmp_path):
        with pytest.raises(ValueError, match="existing directory"):
            change_impact_context(root=tmp_path / "nope", target="x.py")

    def test_isolated_file_no_references(self, tmp_path):
        """A file with no importers or callers."""
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "isolated.py").write_text("def lonely(): pass\n")
        (pkg / "other.py").write_text("x = 1\n")
        ctx = change_impact_context(
            root=tmp_path,
            target="pkg/isolated.py",
            function="lonely",
        )
        assert ctx["metrics"]["importer_count"] == 0
        assert ctx["metrics"]["call_site_count"] == 0

    def test_include_docs_false(self):
        ctx = change_impact_context(
            root=PROJECT_ROOT,
            target="src/odibi_anchor/debugging/error_trace_context.py",
            function="error_trace_context",
            include_docs=False,
        )
        assert ctx["metrics"]["doc_reference_count"] == 0
