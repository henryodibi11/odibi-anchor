"""Tests for preflight baseline (diff-only error reporting).

Verifies that _error_fingerprint produces stable IDs and that baseline
filtering correctly separates pre-existing errors from new ones.
"""

import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch

from odibi_anchor._repository_snapshot import ChangedLineRange

# Load preflight_context module directly (bypasses __init__.py chain)
_SPEC = importlib.util.spec_from_file_location(
    "preflight_context",
    str(Path(__file__).resolve().parents[1] / "src" / "odibi_anchor" / "codebase" / "preflight_context.py"),
)
_MOD = importlib.util.module_from_spec(_SPEC)
sys.modules["preflight_context"] = _MOD
_SPEC.loader.exec_module(_MOD)

_error_fingerprint = _MOD._error_fingerprint
attribute_diagnostic = _MOD.attribute_diagnostic
preflight_context = _MOD.preflight_context


# ── Test Fixtures ──

DIAG_ERROR_1 = {
    "file": "/project/src/odibi_anchor/codebase/codebase_map_context.py",
    "line": 177,
    "column": 12,
    "message": "Cannot access attribute \"get\" for class \"str\"",
    "severity": "error",
    "source": "pyright",
    "rule": "reportAttributeAccessIssue",
}

DIAG_ERROR_1_SHIFTED = {
    **DIAG_ERROR_1,
    "line": 250,  # Different line (after edits)
    "column": 8,  # Different column
}

DIAG_ERROR_2 = {
    "file": "/project/src/odibi_anchor/codebase/codebase_map_context.py",
    "line": 42,
    "column": 5,
    "message": "Variable \"dbutils\" is not defined",
    "severity": "error",
    "source": "pyright",
    "rule": "reportUndefinedVariable",
}

DIAG_ERROR_NEW = {
    "file": "/project/src/odibi_anchor/codebase/preflight_context.py",
    "line": 115,
    "column": 9,
    "message": "Argument of type \"set[str]\" cannot be assigned to parameter",
    "severity": "error",
    "source": "pyright",
    "rule": "reportArgumentType",
}


# ── Tests ──


class TestErrorFingerprint:
    """Tests for _error_fingerprint stability."""

    def test_fingerprint_stability_across_line_shifts(self):
        """Same error with different line number → same fingerprint."""
        fp1 = _error_fingerprint(DIAG_ERROR_1)
        fp2 = _error_fingerprint(DIAG_ERROR_1_SHIFTED)
        assert fp1 == fp2, f"Fingerprints should match: {fp1!r} != {fp2!r}"

    def test_fingerprint_different_errors_differ(self):
        """Different errors → different fingerprints."""
        fp1 = _error_fingerprint(DIAG_ERROR_1)
        fp2 = _error_fingerprint(DIAG_ERROR_2)
        assert fp1 != fp2

    def test_fingerprint_uses_basename_not_full_path(self):
        """Fingerprint uses file basename, not full path."""
        diag_a = {**DIAG_ERROR_1, "file": "/long/path/to/foo.py"}
        diag_b = {**DIAG_ERROR_1, "file": "/other/path/foo.py"}
        assert _error_fingerprint(diag_a) == _error_fingerprint(diag_b)


class TestPreflightBaseline:
    """Tests for baseline filtering in preflight_context."""

    @patch("preflight_context.shutil.which", return_value="/usr/bin/fake")
    @patch("preflight_context._run_pyright")
    @patch("preflight_context._run_ruff")
    def test_first_run_captures_baseline(self, mock_ruff, mock_pyright, mock_which, tmp_path):
        """First run (no baseline) returns _captured_baseline in metrics."""
        mock_pyright.return_value = [DIAG_ERROR_1, DIAG_ERROR_2]
        mock_ruff.return_value = []

        # Create a dummy .py file so check_syntax has something
        (tmp_path / "dummy.py").write_text("x = 1\n")

        result = preflight_context(
            tmp_path,
            changed_files=["dummy.py"],
            check_types=True,
            check_lint=False,
            check_syntax=False,
            baseline=None,  # First run — no baseline
            output_format="dict",
        )

        # Should capture baseline as sorted list (JSON-safe)
        assert "_captured_baseline" in result["metrics"]
        baseline = result["metrics"]["_captured_baseline"]
        assert isinstance(baseline, list)
        assert len(baseline) == 2

    @patch("preflight_context.shutil.which", return_value="/usr/bin/fake")
    @patch("preflight_context._run_pyright")
    @patch("preflight_context._run_ruff")
    def test_subsequent_run_filters_baseline_errors(self, mock_ruff, mock_pyright, mock_which, tmp_path):
        """Pre-existing errors in baseline are non-blocking."""
        mock_pyright.return_value = [DIAG_ERROR_1, DIAG_ERROR_2]
        mock_ruff.return_value = []

        (tmp_path / "dummy.py").write_text("x = 1\n")

        # Build baseline from the same errors
        baseline = {_error_fingerprint(DIAG_ERROR_1), _error_fingerprint(DIAG_ERROR_2)}

        result = preflight_context(
            tmp_path,
            changed_files=["dummy.py"],
            check_types=True,
            check_lint=False,
            check_syntax=False,
            baseline=baseline,
            output_format="dict",
        )

        assert result["metrics"]["is_safe"] is True
        assert result["metrics"]["baseline_errors"] == 2
        assert result["metrics"]["new_errors"] == 0

    @patch("preflight_context.shutil.which", return_value="/usr/bin/fake")
    @patch("preflight_context._run_pyright")
    @patch("preflight_context._run_ruff")
    def test_new_error_still_blocks(self, mock_ruff, mock_pyright, mock_which, tmp_path):
        """New error introduced after baseline → is_safe=False."""
        # Return baseline errors PLUS a new error
        mock_pyright.return_value = [DIAG_ERROR_1, DIAG_ERROR_2, DIAG_ERROR_NEW]
        mock_ruff.return_value = []

        (tmp_path / "dummy.py").write_text("x = 1\n")

        # Baseline only has the first two
        baseline = {_error_fingerprint(DIAG_ERROR_1), _error_fingerprint(DIAG_ERROR_2)}

        result = preflight_context(
            tmp_path,
            changed_files=["dummy.py"],
            check_types=True,
            check_lint=False,
            check_syntax=False,
            baseline=baseline,
            output_format="dict",
        )

        assert result["metrics"]["is_safe"] is False
        assert result["metrics"]["new_errors"] == 1
        assert result["metrics"]["baseline_errors"] == 2

    @patch("preflight_context.shutil.which", return_value="/usr/bin/fake")
    @patch("preflight_context._run_pyright")
    @patch("preflight_context._run_ruff")
    def test_reset_baseline_clears(self, mock_ruff, mock_pyright, mock_which, tmp_path):
        """After reset, passing None baseline means all errors are 'new' in capture."""
        mock_pyright.return_value = [DIAG_ERROR_1]
        mock_ruff.return_value = []

        (tmp_path / "dummy.py").write_text("x = 1\n")

        # With baseline=None (simulates reset), should capture fresh baseline
        result = preflight_context(
            tmp_path,
            changed_files=["dummy.py"],
            check_types=True,
            check_lint=False,
            check_syntax=False,
            baseline=None,
            output_format="dict",
        )

        # Captures baseline (first-run behavior after reset) as sorted list
        assert "_captured_baseline" in result["metrics"]
        assert isinstance(result["metrics"]["_captured_baseline"], list)
        assert len(result["metrics"]["_captured_baseline"]) == 1

    @patch("preflight_context.shutil.which", return_value="/usr/bin/fake")
    @patch("preflight_context._run_pyright")
    def test_changed_line_attribution_blocks_introduced_and_uncertain_only(
        self, mock_pyright, mock_which, tmp_path,
    ):
        source = tmp_path / "dummy.py"
        source.write_text("one\ntwo\nthree\n", encoding="utf-8")
        mock_pyright.return_value = [
            {**DIAG_ERROR_1, "file": str(source), "line": 1},
            {**DIAG_ERROR_2, "file": str(source), "line": 2},
            {**DIAG_ERROR_NEW, "file": "", "line": 0},
        ]
        result = preflight_context(
            tmp_path, changed_files=["dummy.py"], check_types=True,
            check_lint=False, check_syntax=False,
            changed_line_ranges=(ChangedLineRange("dummy.py", "modified", 2, 2),),
            output_format="dict",
        )
        assert result["metrics"]["baseline_errors"] == 1
        assert result["metrics"]["new_errors"] == 2
        assert result["metrics"]["is_safe"] is False

    def test_shared_attribution_handles_created_deleted_and_unknown_ranges(self, tmp_path):
        created = tmp_path / "new.py"
        created.write_text("x = 1\n", encoding="utf-8")
        diagnostic = {**DIAG_ERROR_NEW, "file": str(created), "line": 1}

        assert attribute_diagnostic(
            diagnostic, tmp_path,
            changed_paths={"new.py"}, ranges_by_path={}, created_paths={"new.py"},
        ) == "introduced"
        assert attribute_diagnostic(
            diagnostic, tmp_path,
            changed_paths={"new.py"}, ranges_by_path={}, created_paths=set(),
        ) == "uncertain"
        assert attribute_diagnostic(
            diagnostic, tmp_path,
            changed_paths={"new.py"},
            ranges_by_path={"new.py": [(1, 1, "deleted")]}, created_paths=set(),
        ) == "pre_existing"

    @patch("preflight_context.shutil.which", return_value=None)
    def test_unavailable_analyzers_are_explicit_not_passing_evidence(self, mock_which, tmp_path):
        (tmp_path / "dummy.py").write_text("x = 1\n", encoding="utf-8")

        result = preflight_context(
            tmp_path, changed_files=["dummy.py"], check_types=True, check_lint=True,
            check_syntax=False, output_format="dict",
        )

        assert result["metrics"]["tools_available"] == {"pyright": False, "ruff": False}
        assert result["metrics"]["total_diagnostics"] == 0
