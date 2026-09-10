"""Tests for codebase_map_context — dog-food against odibi_anchor itself.

Tests verify:
- Module discovery and AST parsing
- Function/class extraction
- Test coverage mapping
- Dependency graph construction
- Metrics computation
- Render function output
- output_format dispatch
- Edge cases (empty dir, parse errors, max_files)
"""

import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from odibi_anchor.codebase import codebase_map_context, render_codebase_map_report


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

ODIBI_ANCHOR_ROOT = Path(__file__).resolve().parent.parent
ODIBI_ANCHOR_SRC = ODIBI_ANCHOR_ROOT / "src" / "odibi_anchor"


@pytest.fixture
def project_ctx():
    """Full project-level scan (includes tests)."""
    return codebase_map_context(ODIBI_ANCHOR_ROOT, subject="odibi_anchor")


@pytest.fixture
def src_ctx():
    """Source-only scan (no test mapping)."""
    return codebase_map_context(ODIBI_ANCHOR_SRC, subject="anchor_src", include_tests=False)


@pytest.fixture
def tiny_package(tmp_path):
    """Create a minimal package for isolated testing."""
    pkg = tmp_path / "my_pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(
        '''"""My package."""\n\nfrom my_pkg.core import hello\n\n__all__ = ["hello"]\n'''
    )
    (pkg / "core.py").write_text(
        '''"""Core module."""\n\ndef hello(name: str = "world") -> str:\n    """Say hello."""\n    return f"Hello, {name}"\n\n\ndef _private():\n    pass\n'''
    )
    (pkg / "utils.py").write_text(
        '''"""Utilities."""\n\nfrom my_pkg.core import hello\n\ndef helper(x: int) -> int:\n    return x + 1\n'''
    )
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "__init__.py").write_text("")
    (tests / "test_core.py").write_text(
        '''"""Tests for core."""\nfrom my_pkg.core import hello\n\ndef test_hello():\n    assert hello() == "Hello, world"\n'''
    )
    return tmp_path


# ===========================================================================
# Basic Contract Tests
# ===========================================================================

class TestOutputContract:
    """Verify output follows the standard context contract."""

    def test_kind(self, project_ctx):
        assert project_ctx["kind"] == "codebase_map_context"

    def test_subject(self, project_ctx):
        assert project_ctx["subject"] == "odibi_anchor"

    def test_summary_is_string(self, project_ctx):
        assert isinstance(project_ctx["summary"], str)
        assert "modules" in project_ctx["summary"]

    def test_has_standard_keys(self, project_ctx):
        required = {"kind", "subject", "summary", "metrics", "findings", "risks", "suggested_next_actions", "samples"}
        assert required.issubset(project_ctx.keys())

    def test_metrics_is_dict(self, project_ctx):
        assert isinstance(project_ctx["metrics"], dict)

    def test_findings_is_list(self, project_ctx):
        assert isinstance(project_ctx["findings"], list)
        assert len(project_ctx["findings"]) > 0

    def test_risks_is_list(self, project_ctx):
        assert isinstance(project_ctx["risks"], list)


# ===========================================================================
# Metrics Tests (dog-food on odibi_anchor)
# ===========================================================================

class TestMetrics:
    """Verify metrics against known odibi_anchor structure."""

    def test_module_count(self, project_ctx):
        # We know odibi_anchor has at least 6 packages
        assert project_ctx["metrics"]["module_count"] >= 6

    def test_public_function_count(self, project_ctx):
        # At least the 10 main tools + render functions
        assert project_ctx["metrics"]["public_function_count"] >= 20

    def test_total_lines(self, project_ctx):
        # We know it's ~15K lines
        assert project_ctx["metrics"]["total_source_lines"] > 10000

    def test_test_file_count(self, project_ctx):
        # At least 10 test files
        assert project_ctx["metrics"]["test_file_count"] >= 10

    def test_zero_parse_errors(self, project_ctx):
        assert project_ctx["metrics"]["parse_error_count"] == 0

    def test_test_coverage_nonzero(self, project_ctx):
        assert project_ctx["metrics"]["test_coverage_pct"] > 0


# ===========================================================================
# Module Discovery
# ===========================================================================

class TestModuleDiscovery:
    """Verify correct module/file discovery."""

    def test_finds_key_files(self, project_ctx):
        paths = list(project_ctx["modules"].keys())
        # Should find core files
        assert any("quality_gate_context.py" in p for p in paths)
        assert any("error_trace_context.py" in p for p in paths)
        assert any("codebase_map_context.py" in p for p in paths)

    def test_excludes_test_files_from_modules(self, project_ctx):
        paths = list(project_ctx["modules"].keys())
        assert not any(Path(p).name.startswith("test_") or Path(p).name.endswith("_test.py") for p in paths)

    def test_excludes_pycache(self, project_ctx):
        paths = list(project_ctx["modules"].keys())
        assert not any("__pycache__" in p for p in paths)


# ===========================================================================
# Function Extraction
# ===========================================================================

class TestFunctionExtraction:
    """Verify AST-based function extraction."""

    def test_extracts_public_functions(self, project_ctx):
        # Find quality_gate_context module
        qg_mod = None
        for path, mod in project_ctx["modules"].items():
            if "quality_gate_context.py" in path:
                qg_mod = mod
                break
        assert qg_mod is not None
        func_names = [f["name"] for f in qg_mod["functions"]]
        assert "quality_gate_context" in func_names
        assert "render_quality_gate_report" in func_names

    def test_excludes_private_by_default(self, project_ctx):
        for path, mod in project_ctx["modules"].items():
            for func in mod.get("functions", []):
                assert not func["name"].startswith("_")

    def test_function_has_signature(self, project_ctx):
        for path, mod in project_ctx["modules"].items():
            for func in mod.get("functions", []):
                assert "signature" in func
                assert func["signature"].startswith("(")

    def test_function_has_line_number(self, project_ctx):
        for path, mod in project_ctx["modules"].items():
            for func in mod.get("functions", []):
                assert "line" in func
                assert isinstance(func["line"], int)
                assert func["line"] > 0


# ===========================================================================
# Include Private
# ===========================================================================

class TestIncludePrivate:
    """Test include_private=True option."""

    def test_includes_private_when_enabled(self, src_ctx):
        # Default excludes private
        for path, mod in src_ctx["modules"].items():
            for func in mod.get("functions", []):
                assert not func["name"].startswith("_")

    def test_private_visible(self):
        ctx = codebase_map_context(
            ODIBI_ANCHOR_SRC,
            include_private=True,
            include_tests=False,
        )
        has_private = False
        for path, mod in ctx["modules"].items():
            for func in mod.get("functions", []):
                if func["name"].startswith("_"):
                    has_private = True
                    break
        assert has_private


# ===========================================================================
# Test Mapping
# ===========================================================================

class TestTestMapping:
    """Verify test-to-source mapping."""

    def test_quality_gate_has_tests(self, project_ctx):
        found = False
        for src, tests in project_ctx["test_map"].items():
            if "quality_gate_context.py" in src:
                found = True
                assert any("test_quality_gate" in t for t in tests)
        assert found

    def test_error_trace_has_tests(self, project_ctx):
        found = False
        for src, tests in project_ctx["test_map"].items():
            if "error_trace_context.py" in src:
                found = True
                assert any("test_error_trace" in t or "test_render" in t for t in tests)
        assert found


# ===========================================================================
# Dependency Graph
# ===========================================================================

class TestDependencyGraph:
    """Verify internal dependency detection."""

    def test_graph_is_dict(self, project_ctx):
        assert isinstance(project_ctx["dependency_graph"], dict)

    def test_validation_depends_on_engine_utils(self, project_ctx):
        for src, deps in project_ctx["dependency_graph"].items():
            if "quality_gate_context" in src:
                dep_stems = [Path(d).stem for d in deps]
                assert "engine_utils" in dep_stems


# ===========================================================================
# Exports
# ===========================================================================

class TestExports:
    """Verify exports map."""

    def test_exports_from_init(self, project_ctx):
        assert len(project_ctx["exports"]) > 0

    def test_validation_exports(self, project_ctx):
        for init_path, symbols in project_ctx["exports"].items():
            if "validation" in init_path and "__init__" in init_path:
                assert "quality_gate_context" in symbols
                assert "duplicate_key_context" in symbols


# ===========================================================================
# Render Function
# ===========================================================================

class TestRenderCodebaseMapReport:
    """Test markdown rendering."""

    def test_returns_string(self, project_ctx):
        result = render_codebase_map_report(project_ctx)
        assert isinstance(result, str)

    def test_contains_header(self, project_ctx):
        result = render_codebase_map_report(project_ctx)
        assert "# Codebase Map: odibi_anchor" in result

    def test_contains_metrics_table(self, project_ctx):
        result = render_codebase_map_report(project_ctx)
        assert "## Metrics" in result
        assert "| Metric | Value |" in result

    def test_contains_modules_section(self, project_ctx):
        result = render_codebase_map_report(project_ctx)
        assert "## Modules" in result

    def test_contains_function_signatures(self, project_ctx):
        result = render_codebase_map_report(project_ctx)
        assert "## Function Signatures" in result
        assert "quality_gate_context" in result

    def test_contains_test_map(self, project_ctx):
        result = render_codebase_map_report(project_ctx)
        assert "## Test Coverage Map" in result

    def test_missing_keys_raises(self):
        with pytest.raises(ValueError, match="Missing required keys"):
            render_codebase_map_report({"kind": "codebase_map_context"})


# ===========================================================================
# output_format Dispatch
# ===========================================================================

class TestOutputFormat:
    """Test output_format parameter."""

    def test_dict_default(self):
        ctx = codebase_map_context(ODIBI_ANCHOR_SRC, include_tests=False)
        assert isinstance(ctx, dict)

    def test_markdown_mode(self):
        result = codebase_map_context(
            ODIBI_ANCHOR_SRC, include_tests=False, output_format="markdown"
        )
        assert isinstance(result, str)
        assert "# Codebase Map:" in result

    def test_invalid_raises(self):
        with pytest.raises(ValueError, match="output_format"):
            codebase_map_context(ODIBI_ANCHOR_SRC, output_format="xml")


# ===========================================================================
# Edge Cases
# ===========================================================================

class TestEdgeCases:
    """Edge case handling."""

    def test_nonexistent_dir_raises(self, tmp_path):
        with pytest.raises(ValueError, match="existing directory"):
            codebase_map_context(tmp_path / "nope")

    def test_empty_directory(self, tmp_path):
        empty = tmp_path / "empty_pkg"
        empty.mkdir()
        ctx = codebase_map_context(empty, subject="empty")
        assert ctx["metrics"]["module_count"] == 0
        assert ctx["metrics"]["public_function_count"] == 0

    def test_max_files_exceeded(self, tmp_path):
        pkg = tmp_path / "big"
        pkg.mkdir()
        for i in range(6):
            (pkg / f"mod_{i}.py").write_text(f"x = {i}")
        with pytest.raises(ValueError, match="max_files"):
            codebase_map_context(pkg, max_files=5)

    def test_syntax_error_file(self, tmp_path):
        pkg = tmp_path / "bad_pkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "broken.py").write_text("def oops(\n    pass")
        ctx = codebase_map_context(pkg, subject="bad")
        assert ctx["metrics"]["parse_error_count"] == 1


# ===========================================================================
# Tiny Package (isolated)
# ===========================================================================

class TestTinyPackage:
    """Test against a controlled minimal package."""

    def test_finds_core_function(self, tiny_package):
        ctx = codebase_map_context(tiny_package, subject="my_pkg")
        func_names = []
        for mod in ctx["modules"].values():
            for f in mod.get("functions", []):
                func_names.append(f["name"])
        assert "hello" in func_names
        assert "helper" in func_names

    def test_private_excluded(self, tiny_package):
        ctx = codebase_map_context(tiny_package, subject="my_pkg")
        func_names = []
        for mod in ctx["modules"].values():
            for f in mod.get("functions", []):
                func_names.append(f["name"])
        assert "_private" not in func_names

    def test_exports_detected(self, tiny_package):
        ctx = codebase_map_context(tiny_package, subject="my_pkg")
        all_exports = []
        for syms in ctx["exports"].values():
            all_exports.extend(syms)
        assert "hello" in all_exports

    def test_test_map_built(self, tiny_package):
        ctx = codebase_map_context(tiny_package, subject="my_pkg")
        # test_core.py should map to core.py
        mapped_sources = list(ctx["test_map"].keys())
        assert any("core.py" in s for s in mapped_sources)

    def test_dependency_graph(self, tiny_package):
        ctx = codebase_map_context(tiny_package, subject="my_pkg")
        # utils imports from core
        for src, deps in ctx["dependency_graph"].items():
            if "utils" in src:
                assert any("core" in d for d in deps)

    def test_function_signature(self, tiny_package):
        ctx = codebase_map_context(tiny_package, subject="my_pkg")
        for mod in ctx["modules"].values():
            for f in mod.get("functions", []):
                if f["name"] == "hello":
                    assert "name: str" in f["signature"]
                    assert f["returns"] == "str"
                    assert f["docstring"] == "Say hello."



# ===========================================================================
# New Feature Tests: end_line, return_keys, focus_file, full docstrings
# ===========================================================================

class TestEndLine:
    """Tests for end_line and line_count on functions."""

    def test_end_line_present(self, project_ctx):
        for mod in project_ctx["modules"].values():
            for func in mod.get("functions", []):
                assert "end_line" in func
                assert "line_count" in func

    def test_end_line_greater_than_start(self, project_ctx):
        for mod in project_ctx["modules"].values():
            for func in mod.get("functions", []):
                if func["end_line"] is not None:
                    assert func["end_line"] >= func["line"]

    def test_line_count_correct(self, project_ctx):
        for mod in project_ctx["modules"].values():
            for func in mod.get("functions", []):
                if func["end_line"] is not None:
                    expected = func["end_line"] - func["line"] + 1
                    assert func["line_count"] == expected

    def test_end_line_on_tiny_package(self, tiny_package):
        ctx = codebase_map_context(tiny_package, subject="test")
        for mod in ctx["modules"].values():
            for func in mod.get("functions", []):
                if func["name"] == "hello":
                    assert func["line_count"] >= 2  # At least def + body


class TestReturnKeys:
    """Tests for return_keys extraction from dict returns."""

    def test_error_trace_has_return_keys(self, src_ctx):
        for mod in src_ctx["modules"].values():
            for func in mod.get("functions", []):
                if func["name"] == "error_trace_context":
                    assert "return_keys" in func
                    assert "kind" in func["return_keys"]
                    assert "summary" in func["return_keys"]
                    assert "metrics" in func["return_keys"]

    def test_return_keys_not_present_when_no_dict_return(self, src_ctx):
        """Functions that don't return dicts should not have return_keys."""
        for mod in src_ctx["modules"].values():
            for func in mod.get("functions", []):
                if func["name"] == "render_codebase_map_report":
                    # Returns str, not dict
                    assert "return_keys" not in func

    def test_extract_return_keys_disabled(self):
        ctx = codebase_map_context(
            ODIBI_ANCHOR_SRC,
            include_tests=False,
            extract_return_keys=False,
        )
        for mod in ctx["modules"].values():
            for func in mod.get("functions", []):
                assert "return_keys" not in func


class TestFullDocstrings:
    """Tests for max_docstring_chars parameter."""

    def test_default_500_chars(self, src_ctx):
        for mod in src_ctx["modules"].values():
            for func in mod.get("functions", []):
                if func["docstring"]:
                    assert len(func["docstring"]) <= 503  # 500 + "..."

    def test_custom_max_docstring(self):
        ctx = codebase_map_context(
            ODIBI_ANCHOR_SRC,
            include_tests=False,
            max_docstring_chars=100,
        )
        for mod in ctx["modules"].values():
            for func in mod.get("functions", []):
                if func["docstring"]:
                    assert len(func["docstring"]) <= 103


class TestFocusFile:
    """Tests for focus_file deep-dive parameter."""

    def test_focus_present_when_set(self):
        ctx = codebase_map_context(
            ODIBI_ANCHOR_SRC,
            include_tests=False,
            focus_file="validation/quality_gate_context.py",
        )
        assert ctx["focus"] is not None
        assert ctx["focus"]["path"] == "validation/quality_gate_context.py"

    def test_focus_none_when_not_set(self, src_ctx):
        assert src_ctx["focus"] is None

    def test_focus_includes_private_functions(self):
        ctx = codebase_map_context(
            ODIBI_ANCHOR_SRC,
            include_tests=False,
            focus_file="validation/quality_gate_context.py",
        )
        func_names = [f["name"] for f in ctx["focus"]["functions"]]
        # Should include private helpers
        assert any(n.startswith("_") for n in func_names)

    def test_focus_includes_constants(self):
        ctx = codebase_map_context(
            ODIBI_ANCHOR_SRC,
            include_tests=False,
            focus_file="validation/quality_gate_context.py",
        )
        assert len(ctx["focus"]["constants"]) > 0
        const_names = [c["name"] for c in ctx["focus"]["constants"]]
        assert "_ALL_CHECKS" in const_names

    def test_focus_unlimited_docstrings(self):
        ctx = codebase_map_context(
            ODIBI_ANCHOR_SRC,
            include_tests=False,
            focus_file="validation/quality_gate_context.py",
        )
        # Focus mode uses max_docstring_chars=0 (unlimited)
        for func in ctx["focus"]["functions"]:
            if func["name"] == "quality_gate_context":
                # Full docstring should be very long (> 500 chars)
                assert func["docstring"] is not None
                assert len(func["docstring"]) > 500
                break

    def test_focus_nonexistent_file(self):
        ctx = codebase_map_context(
            ODIBI_ANCHOR_SRC,
            include_tests=False,
            focus_file="nonexistent.py",
        )
        assert "error" in ctx["focus"]

    def test_focus_has_imports(self):
        ctx = codebase_map_context(
            ODIBI_ANCHOR_SRC,
            include_tests=False,
            focus_file="debugging/error_trace_context.py",
        )
        assert len(ctx["focus"]["imports"]) > 0

    def test_focus_has_line_ranges(self):
        ctx = codebase_map_context(
            ODIBI_ANCHOR_SRC,
            include_tests=False,
            focus_file="debugging/error_trace_context.py",
        )
        for func in ctx["focus"]["functions"]:
            assert func["end_line"] is not None
            assert func["line_count"] is not None



# ---------------------------------------------------------------------------
# Tests for MAP_DEEPEN_SPEC features (reverse graph, blast radius, dead code)
# ---------------------------------------------------------------------------


class TestBuildReverseGraph:
    """Tests for _build_reverse_graph — pure inversion of forward graph."""

    def test_empty_graph(self):
        """Empty forward graph produces empty reverse graph."""
        from odibi_anchor.codebase.codebase_map_context import _build_reverse_graph
        assert _build_reverse_graph({}) == {}

    def test_single_dependency(self):
        """A -> B produces B -> [A]."""
        from odibi_anchor.codebase.codebase_map_context import _build_reverse_graph
        forward = {"a.py": ["b.py"]}
        reverse = _build_reverse_graph(forward)
        assert reverse == {"b.py": ["a.py"]}

    def test_multiple_importers(self):
        """Multiple modules importing the same dep are all listed."""
        from odibi_anchor.codebase.codebase_map_context import _build_reverse_graph
        forward = {
            "a.py": ["core.py"],
            "b.py": ["core.py"],
            "c.py": ["core.py", "utils.py"],
        }
        reverse = _build_reverse_graph(forward)
        assert sorted(reverse["core.py"]) == ["a.py", "b.py", "c.py"]
        assert reverse["utils.py"] == ["c.py"]

    def test_no_reverse_for_leaf_modules(self):
        """Modules that import others but are never imported have no reverse entry."""
        from odibi_anchor.codebase.codebase_map_context import _build_reverse_graph
        forward = {"app.py": ["lib.py"]}
        reverse = _build_reverse_graph(forward)
        assert "app.py" not in reverse

    def test_chain_dependency(self):
        """A -> B -> C produces correct reverse graph."""
        from odibi_anchor.codebase.codebase_map_context import _build_reverse_graph
        forward = {"a.py": ["b.py"], "b.py": ["c.py"]}
        reverse = _build_reverse_graph(forward)
        assert reverse == {"b.py": ["a.py"], "c.py": ["b.py"]}

    def test_integration_with_real_project(self, project_ctx):
        """Real project scan produces a non-empty reverse graph."""
        assert "reverse_dependency_graph" in project_ctx
        rev = project_ctx["reverse_dependency_graph"]
        # Should have at least one entry (the project has internal imports)
        assert len(rev) > 0


class TestComputeBlastRadius:
    """Tests for _compute_blast_radius — BFS over reverse graph."""

    def test_no_dependents(self):
        """File with no dependents has zero blast radius."""
        from odibi_anchor.codebase.codebase_map_context import _compute_blast_radius
        reverse = {"other.py": ["app.py"]}
        result = _compute_blast_radius("lonely.py", reverse)
        assert result["direct_callers"] == []
        assert result["transitive_callers"] == []
        assert result["total_affected"] == 0
        assert result["severity"] == "low"

    def test_single_direct_caller(self):
        """One direct caller, no transitive — low severity."""
        from odibi_anchor.codebase.codebase_map_context import _compute_blast_radius
        reverse = {"core.py": ["app.py"]}
        result = _compute_blast_radius("core.py", reverse)
        assert result["direct_callers"] == ["app.py"]
        assert result["transitive_callers"] == []
        assert result["total_affected"] == 1
        assert result["severity"] == "low"

    def test_medium_severity(self):
        """2-4 affected files → medium severity."""
        from odibi_anchor.codebase.codebase_map_context import _compute_blast_radius
        reverse = {"core.py": ["a.py", "b.py"]}
        result = _compute_blast_radius("core.py", reverse)
        assert result["total_affected"] == 2
        assert result["severity"] == "medium"

    def test_high_severity_transitive(self):
        """5+ affected files (transitively) → high severity."""
        from odibi_anchor.codebase.codebase_map_context import _compute_blast_radius
        reverse = {
            "core.py": ["a.py", "b.py", "c.py"],
            "a.py": ["d.py", "e.py"],
        }
        result = _compute_blast_radius("core.py", reverse)
        assert set(result["direct_callers"]) == {"a.py", "b.py", "c.py"}
        assert set(result["transitive_callers"]) == {"d.py", "e.py"}
        assert result["total_affected"] == 5
        assert result["severity"] == "high"

    def test_no_cycles(self):
        """BFS doesn't loop on circular dependencies."""
        from odibi_anchor.codebase.codebase_map_context import _compute_blast_radius
        reverse = {"a.py": ["b.py"], "b.py": ["a.py"]}
        result = _compute_blast_radius("a.py", reverse)
        # b.py is direct caller, a.py appears as transitive (caller of b.py)
        # BFS correctly terminates without infinite loop
        assert result["direct_callers"] == ["b.py"]
        assert result["total_affected"] == 2  # b.py + a.py (self via transitive)

    def test_integration_with_focus(self):
        """focus_file populates blast_radius in focus dict."""
        ctx = codebase_map_context(
            ODIBI_ANCHOR_ROOT,
            subject="odibi_anchor",
            focus_file="src/odibi_anchor/_utils/contract.py",
        )
        assert ctx["focus"] is not None
        assert "blast_radius" in ctx["focus"]
        br = ctx["focus"]["blast_radius"]
        assert "direct_callers" in br
        assert "severity" in br


class TestDetectDeadCode:
    """Tests for _detect_dead_code — unreferenced public functions."""

    def test_empty_modules(self):
        """No modules → no dead code."""
        from odibi_anchor.codebase.codebase_map_context import _detect_dead_code
        assert _detect_dead_code({}, {}, {}) == []

    def test_module_with_importers_is_not_dead(self):
        """Modules that are imported by others are never dead."""
        from odibi_anchor.codebase.codebase_map_context import _detect_dead_code
        modules = {"core.py": {"functions": [{"name": "hello"}]}}
        reverse_graph = {"core.py": ["app.py"]}
        result = _detect_dead_code(modules, reverse_graph, {})
        assert result == []

    def test_private_function_excluded(self):
        """Private functions (leading _) are never flagged."""
        from odibi_anchor.codebase.codebase_map_context import _detect_dead_code
        modules = {"orphan.py": {"functions": [{"name": "_internal"}]}}
        result = _detect_dead_code(modules, {}, {})
        assert result == []

    def test_exported_function_excluded(self):
        """Functions in __all__ exports are not dead."""
        from odibi_anchor.codebase.codebase_map_context import _detect_dead_code
        modules = {"orphan.py": {"functions": [{"name": "hello"}]}}
        exports = {"__init__.py": ["hello"]}
        result = _detect_dead_code(modules, {}, exports)
        assert result == []

    def test_unreferenced_public_function_detected(self):
        """Public function in unreferenced module → dead code candidate."""
        from odibi_anchor.codebase.codebase_map_context import _detect_dead_code
        modules = {"orphan.py": {"functions": [{"name": "unused_fn"}]}}
        result = _detect_dead_code(modules, {}, {})
        assert len(result) == 1
        assert result[0]["file"] == "orphan.py"
        assert result[0]["function"] == "unused_fn"
        assert "no importers" in result[0]["reason"]

    def test_init_files_skipped(self):
        """__init__.py files are never flagged as dead code."""
        from odibi_anchor.codebase.codebase_map_context import _detect_dead_code
        modules = {"__init__.py": {"functions": [{"name": "setup"}]}}
        result = _detect_dead_code(modules, {}, {})
        assert result == []

    def test_integration_return_key(self, project_ctx):
        """Real project returns dead_code_candidates key."""
        assert "dead_code_candidates" in project_ctx
        # Type check
        assert isinstance(project_ctx["dead_code_candidates"], list)


class TestContextualSuggestions:
    """Tests for the revamped _build_suggestions — contextual not hardcoded."""

    def test_no_hardcoded_boilerplate(self, project_ctx):
        """Suggestions should NOT contain the old hardcoded strings."""
        suggestions = project_ctx["suggested_next_actions"]
        old_strings = [
            "Use ctx['modules'][path]['functions'] to find function signatures",
            "Use ctx['exports'] to see the public API surface",
            "Use ctx['dependency_graph'] to understand import relationships",
        ]
        for old in old_strings:
            assert not any(old in s for s in suggestions), f"Found hardcoded string: {old}"

    def test_untested_modules_flagged(self):
        """Untested modules produce a MUST suggestion."""
        from odibi_anchor.codebase.codebase_map_context import _build_suggestions
        modules = {"a.py": {}, "b.py": {}, "c.py": {}}
        test_map = {"a.py": ["tests/test_a.py"]}  # b.py and c.py untested
        suggestions = _build_suggestions([], [], modules, test_map, {})
        assert any("untested" in s.lower() for s in suggestions)
        assert any("MUST" in s for s in suggestions)

    def test_dead_code_generates_should(self):
        """Dead code candidates generate SHOULD suggestion."""
        from odibi_anchor.codebase.codebase_map_context import _build_suggestions
        dead = [{"file": "orphan.py", "function": "unused"}]
        suggestions = _build_suggestions([], [], {}, {}, {}, dead_code=dead)
        assert any("dead-code" in s.lower() for s in suggestions)
        assert any("SHOULD" in s for s in suggestions)

    def test_high_blast_radius_generates_must(self):
        """High blast radius generates MUST: test suggestion."""
        from odibi_anchor.codebase.codebase_map_context import _build_suggestions
        blast = {"severity": "high", "total_affected": 7}
        suggestions = _build_suggestions([], [], {}, {}, {}, blast_radius=blast)
        assert any("anchor('test')" in s for s in suggestions)
        assert any("MUST" in s for s in suggestions)

    def test_hot_modules_flagged(self):
        """Modules with 3+ importers get SHOULD: safe suggestion."""
        from odibi_anchor.codebase.codebase_map_context import _build_suggestions
        reverse = {"core.py": ["a.py", "b.py", "c.py"]}
        suggestions = _build_suggestions([], [], {}, {}, reverse)
        assert any("anchor('safe')" in s for s in suggestions)


class TestReturnDictContract:
    """Tests for the new return dict keys added by MAP_DEEPEN_SPEC."""

    def test_reverse_dependency_graph_key(self, project_ctx):
        """Return dict has reverse_dependency_graph key."""
        assert "reverse_dependency_graph" in project_ctx
        assert isinstance(project_ctx["reverse_dependency_graph"], dict)

    def test_dead_code_candidates_key(self, project_ctx):
        """Return dict has dead_code_candidates key."""
        assert "dead_code_candidates" in project_ctx
        assert isinstance(project_ctx["dead_code_candidates"], list)

    def test_focus_blast_radius(self):
        """focus dict includes blast_radius when focus_file is set."""
        ctx = codebase_map_context(
            ODIBI_ANCHOR_ROOT,
            subject="odibi_anchor",
            focus_file="src/odibi_anchor/_utils/contract.py",
        )
        assert ctx["focus"]["blast_radius"] is not None
        assert "severity" in ctx["focus"]["blast_radius"]

    def test_detect_dead_code_false_skips(self):
        """detect_dead_code=False produces empty dead_code_candidates."""
        ctx = codebase_map_context(
            ODIBI_ANCHOR_ROOT,
            subject="odibi_anchor",
            detect_dead_code=False,
        )
        assert ctx["dead_code_candidates"] == []


    def test_main_guard_excludes_module(self):
        """Modules with if __name__ == '__main__' should not be flagged."""
        from odibi_anchor.codebase.codebase_map_context import _detect_dead_code
        modules = {
            "scripts/run.py": {
                "functions": [{"name": "main"}],
                "has_main_guard": True,
            },
            "src/lib.py": {
                "functions": [{"name": "helper"}],
                "has_main_guard": False,
            },
        }
        reverse_graph = {}  # neither module has importers
        exports = {}
        dead = _detect_dead_code(modules, reverse_graph, exports)
        # scripts/run.py should be excluded, src/lib.py should be flagged
        dead_files = [d["file"] for d in dead]
        assert "scripts/run.py" not in dead_files
        assert "src/lib.py" in dead_files

    def test_has_main_guard_detection(self):
        """_has_main_guard correctly identifies the pattern."""
        from odibi_anchor.codebase.codebase_map_context import _has_main_guard
        import ast
        code_with_guard = 'def main(): pass\nif __name__ == "__main__":\n    main()\n'
        code_without = 'def main(): pass\n'
        tree_with = ast.parse(code_with_guard)
        tree_without = ast.parse(code_without)
        assert _has_main_guard(tree_with) is True
        assert _has_main_guard(tree_without) is False

    def test_has_main_guard_reverse_comparison(self):
        """_has_main_guard handles reversed comparison: '__main__' == __name__."""
        from odibi_anchor.codebase.codebase_map_context import _has_main_guard
        import ast
        code = 'if "__main__" == __name__:\n    pass\n'
        tree = ast.parse(code)
        assert _has_main_guard(tree) is True


    def test_existing_keys_unchanged(self, project_ctx):
        """Existing keys still present and correct type."""
        assert "dependency_graph" in project_ctx
        assert "modules" in project_ctx
        assert "exports" in project_ctx
        assert "test_map" in project_ctx
        assert "metrics" in project_ctx
        assert "findings" in project_ctx
        assert "risks" in project_ctx
