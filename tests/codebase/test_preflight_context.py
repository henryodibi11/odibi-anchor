"""Tests for preflight_context."""

import json
import pytest
from unittest.mock import Mock, patch

from odibi_anchor.codebase.preflight_context import (
    preflight_context,
    render_preflight_report,
    _check_syntax,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def project_with_valid_file(tmp_path):
    """Project with a valid Python file."""
    src = tmp_path / "src"
    src.mkdir()
    f = src / "good.py"
    f.write_text("def hello():\n    return 'world'\n")
    return tmp_path


@pytest.fixture
def project_with_syntax_error(tmp_path):
    """Project with a file containing a syntax error."""
    src = tmp_path / "src"
    src.mkdir()
    f = src / "bad.py"
    f.write_text("def broken(\n    # missing closing paren\n")
    return tmp_path


# ---------------------------------------------------------------------------
# Standard Contract
# ---------------------------------------------------------------------------


class TestStandardContract:
    def test_returns_dict_by_default(self, project_with_valid_file):
        ctx = preflight_context(
            str(project_with_valid_file),
            changed_files=["src/good.py"],
        )
        assert isinstance(ctx, dict)

    def test_has_all_standard_fields(self, project_with_valid_file):
        ctx = preflight_context(
            str(project_with_valid_file),
            changed_files=["src/good.py"],
        )
        required = {"kind", "subject", "summary", "metrics", "findings",
                     "risks", "samples", "suggested_next_actions"}
        assert required.issubset(ctx.keys())

    def test_kind_is_correct(self, project_with_valid_file):
        ctx = preflight_context(
            str(project_with_valid_file),
            changed_files=["src/good.py"],
        )
        assert ctx["kind"] == "preflight_context"

    def test_json_serializable(self, project_with_valid_file):
        ctx = preflight_context(
            str(project_with_valid_file),
            changed_files=["src/good.py"],
        )
        json.dumps(ctx)

    def test_markdown_output(self, project_with_valid_file):
        md = preflight_context(
            str(project_with_valid_file),
            changed_files=["src/good.py"],
            output_format="markdown",
        )
        assert isinstance(md, str)
        assert "Preflight" in md


# ---------------------------------------------------------------------------
# Syntax Checking
# ---------------------------------------------------------------------------


class TestSyntaxCheck:
    def test_syntax_check_valid_file(self, project_with_valid_file):
        """Valid file returns no syntax errors."""
        ctx = preflight_context(
            str(project_with_valid_file),
            changed_files=["src/good.py"],
            check_types=False,
            check_lint=False,
        )
        assert ctx["metrics"]["syntax_errors"] == 0
        assert ctx["metrics"]["is_safe"] is True

    def test_syntax_check_catches_error(self, project_with_syntax_error):
        """File with syntax error is caught."""
        ctx = preflight_context(
            str(project_with_syntax_error),
            changed_files=["src/bad.py"],
            check_types=False,
            check_lint=False,
        )
        assert ctx["metrics"]["syntax_errors"] > 0
        assert ctx["metrics"]["is_safe"] is False

    def test_is_safe_when_no_errors(self, project_with_valid_file):
        """metrics.is_safe True when no errors."""
        ctx = preflight_context(
            str(project_with_valid_file),
            changed_files=["src/good.py"],
            check_types=False,
            check_lint=False,
        )
        assert ctx["metrics"]["is_safe"] is True

    def test_is_safe_false_when_errors(self, project_with_syntax_error):
        """metrics.is_safe False on syntax error."""
        ctx = preflight_context(
            str(project_with_syntax_error),
            changed_files=["src/bad.py"],
            check_types=False,
            check_lint=False,
        )
        assert ctx["metrics"]["is_safe"] is False


# ---------------------------------------------------------------------------
# External Tools Graceful Degradation
# ---------------------------------------------------------------------------


class TestGracefulDegradation:
    def test_tools_not_available_gracefully(self, project_with_valid_file):
        """pyright/ruff missing still produces output."""
        # Even if pyright/ruff not installed, function should work
        ctx = preflight_context(
            str(project_with_valid_file),
            changed_files=["src/good.py"],
        )
        assert ctx["metrics"]["is_safe"] is True
        assert "diagnostics" in ctx

    def test_empty_changed_files(self, tmp_path):
        """Empty changed_files list doesn't crash."""
        with patch("odibi_anchor.codebase.preflight_context.subprocess.run") as run:
            ctx = preflight_context(str(tmp_path), changed_files=[], check_types=True)
        run.assert_not_called()
        assert ctx["metrics"]["is_safe"] is True
        assert ctx["metrics"]["files_checked"] == 0
        assert not ctx["risks"]

    def test_explicit_non_python_scope_invokes_no_checker(self, tmp_path):
        with patch("odibi_anchor.codebase.preflight_context.subprocess.run") as run:
            ctx = preflight_context(
                str(tmp_path), changed_files=["README.md"], check_types=True,
            )
        run.assert_not_called()
        assert ctx["passed"] is True
        assert "No applicable Python" in " ".join(ctx["findings"])
        assert not ctx["risks"]

    def test_mixed_scope_passes_only_python_files(self, tmp_path):
        (tmp_path / "x.py").write_text("x = 1\n")
        ruff = Mock(return_value=[])
        with patch.dict(preflight_context.__globals__, {"_run_ruff": ruff}):
            with patch.object(preflight_context.__globals__["shutil"], "which", return_value="ruff"):
                preflight_context(str(tmp_path), changed_files=["README.md", "x.py"])
        assert [path.name for path in ruff.call_args.args[1]] == ["x.py"]

    def test_omitted_scope_is_repository_wide(self, tmp_path):
        (tmp_path / "x.py").write_text("x = 1\n")
        ctx = preflight_context(str(tmp_path), changed_files=None, check_lint=False)
        assert ctx["metrics"]["scope"] == "repository"
        assert ctx["metrics"]["files_checked"] == 1

    def test_baseline_projection_preserves_raw_counts_and_new_errors(self, tmp_path):
        diagnostics = [
            {"file": f"old{i}.py", "line": 1, "column": 1, "message": "old",
             "severity": "error", "source": "ruff", "rule": f"E{i}"}
            for i in range(120)
        ]
        diagnostics.append(
            {"file": "new.py", "line": 1, "column": 1, "message": "new",
             "severity": "error", "source": "ruff", "rule": "NEW"}
        )
        baseline = {f"ruff:E{i}:old{i}.py" for i in range(120)}
        ruff = Mock(return_value=diagnostics)
        with patch.dict(preflight_context.__globals__, {"_run_ruff": ruff}):
            with patch.object(preflight_context.__globals__["shutil"], "which", return_value="ruff"):
                ctx = preflight_context(str(tmp_path), changed_files=["new.py"], baseline=baseline)
        assert ctx["metrics"]["errors"] == 121
        assert ctx["metrics"]["baseline_errors"] == 120
        assert ctx["diagnostics"][0]["message"] == "new"
        assert ctx["metrics"]["diagnostics_omitted_baseline"] == 120

    def test_baseline_only_errors_are_nonblocking_but_raw_counts_remain(self, tmp_path):
        diagnostic = {
            "file": "old.py", "line": 1, "column": 1, "message": "old",
            "severity": "error", "source": "ruff", "rule": "OLD",
        }
        ruff = Mock(return_value=[diagnostic])
        with patch.dict(preflight_context.__globals__, {"_run_ruff": ruff}):
            with patch.object(preflight_context.__globals__["shutil"], "which", return_value="ruff"):
                ctx = preflight_context(
                    str(tmp_path), changed_files=["old.py"],
                    baseline={"ruff:OLD:old.py"},
                )
        assert ctx["passed"] is True
        assert ctx["metrics"]["is_safe"] is True
        assert ctx["metrics"]["errors"] == 1
        assert ctx["metrics"]["total_diagnostics"] == 1
        assert ctx["metrics"]["baseline_errors"] == 1
        assert ctx["diagnostics"] == []


# ---------------------------------------------------------------------------
# Required Gates Enforcement (Manifest)
# ---------------------------------------------------------------------------


class TestRequiredGates:
    """Tests for required_gates enforcement from .anchor_manifest.json."""

    @pytest.fixture
    def gated_project(self, tmp_path):
        """Project with manifest requiring specific gates."""
        src = tmp_path / "src"
        src.mkdir()
        (src / "good.py").write_text("def hello():\n    return 'world'\n")
        manifest = {
            "project": {"name": "test-project"},
            "constraints": {
                "required_gates": ["preflight", "known_bad"]
            }
        }
        (tmp_path / ".anchor_manifest.json").write_text(json.dumps(manifest))
        return tmp_path

    def test_missing_gates_reported(self, gated_project):
        """Session without required gates should report them as missing."""
        # Mock empty session timings (no gates run yet)
        with patch(
            "odibi_anchor._utils._session_state._SESSION_TIMINGS",
            [],
        ):
            ctx = preflight_context(
                str(gated_project),
                changed_files=["src/good.py"],
            )
        assert len(ctx["metrics"]["missing_required_gates"]) > 0
        assert any("required gate" in f.lower() for f in ctx["findings"])

    def test_all_gates_satisfied(self, gated_project):
        """Session with all required gates should report none missing."""
        mock_timings = [
            {"action": "preflight", "elapsed_ms": 100},
            {"action": "known_bad", "elapsed_ms": 50},
        ]
        with patch(
            "odibi_anchor._utils._session_state._SESSION_TIMINGS",
            mock_timings,
        ):
            ctx = preflight_context(
                str(gated_project),
                changed_files=["src/good.py"],
            )
        assert ctx["metrics"]["missing_required_gates"] == []

    def test_no_manifest_no_gates_check(self, tmp_path):
        """Missing manifest should not add any gate warnings."""
        src = tmp_path / "src"
        src.mkdir()
        (src / "good.py").write_text("def hello():\n    return 'world'\n")
        ctx = preflight_context(
            str(tmp_path),
            changed_files=["src/good.py"],
        )
        assert ctx["metrics"]["missing_required_gates"] == []
