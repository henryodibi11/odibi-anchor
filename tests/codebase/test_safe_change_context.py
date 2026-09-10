"""Tests for safe_change_context."""

import json
import pytest
from unittest.mock import patch

from odibi_anchor.codebase.safe_change_context import (
    safe_change_context,
    render_safe_change_report,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def project_root(tmp_path):
    """Create a minimal project with a target file."""
    src = tmp_path / "src" / "mylib"
    src.mkdir(parents=True)
    target = src / "utils.py"
    target.write_text(
        'import os\n\n\ndef helper(x: int, y: int) -> int:\n'
        '    """Add two numbers."""\n    return x + y\n'
    )
    return tmp_path


@pytest.fixture
def target_rel():
    return "src/mylib/utils.py"


# ---------------------------------------------------------------------------
# Standard Contract
# ---------------------------------------------------------------------------


class TestStandardContract:
    def test_returns_dict_by_default(self, project_root, target_rel):
        with patch("odibi_anchor.codebase.semantic_edit_context._HAS_LIBCST", False):
            ctx = safe_change_context(
                str(project_root), target=target_rel, action="add_parameter",
                function="helper", param_name="verbose",
            )
        assert isinstance(ctx, dict)

    def test_has_all_standard_fields(self, project_root, target_rel):
        with patch("odibi_anchor.codebase.semantic_edit_context._HAS_LIBCST", False):
            ctx = safe_change_context(
                str(project_root), target=target_rel, action="add_parameter",
                function="helper", param_name="verbose",
            )
        required = {"kind", "subject", "summary", "metrics", "findings",
                     "risks", "samples", "suggested_next_actions"}
        assert required.issubset(ctx.keys())

    def test_kind_is_correct(self, project_root, target_rel):
        with patch("odibi_anchor.codebase.semantic_edit_context._HAS_LIBCST", False):
            ctx = safe_change_context(
                str(project_root), target=target_rel, action="add_parameter",
                function="helper", param_name="verbose",
            )
        assert ctx["kind"] == "safe_change_context"

    def test_json_serializable(self, project_root, target_rel):
        with patch("odibi_anchor.codebase.semantic_edit_context._HAS_LIBCST", False):
            ctx = safe_change_context(
                str(project_root), target=target_rel, action="add_parameter",
                function="helper", param_name="verbose",
            )
        json.dumps(ctx)

    def test_markdown_output(self, project_root, target_rel):
        with patch("odibi_anchor.codebase.semantic_edit_context._HAS_LIBCST", False):
            md = safe_change_context(
                str(project_root), target=target_rel, action="add_parameter",
                function="helper", param_name="verbose", output_format="markdown",
            )
        assert isinstance(md, str)
        assert "Safe Change" in md


# ---------------------------------------------------------------------------
# Pipeline Behavior
# ---------------------------------------------------------------------------


class TestPipeline:
    def test_dry_run_by_default(self, project_root, target_rel):
        """apply=False, file unchanged."""
        original = (project_root / target_rel).read_text()
        with patch("odibi_anchor.codebase.semantic_edit_context._HAS_LIBCST", False):
            ctx = safe_change_context(
                str(project_root), target=target_rel, action="add_parameter",
                function="helper", param_name="verbose",
            )
        assert ctx["metrics"]["applied"] is False
        assert (project_root / target_rel).read_text() == original

    def test_chains_edit_and_preflight(self, project_root, target_rel):
        """Both sub-results present."""
        with patch("odibi_anchor.codebase.semantic_edit_context._HAS_LIBCST", False):
            ctx = safe_change_context(
                str(project_root), target=target_rel, action="add_parameter",
                function="helper", param_name="verbose", verify=True, test=False,
            )
        assert ctx["edit"] is not None
        assert ctx["preflight"] is not None

    def test_is_safe_when_all_pass(self, project_root, target_rel):
        """is_safe True when edit succeeds + no preflight errors."""
        with patch("odibi_anchor.codebase.semantic_edit_context._HAS_LIBCST", False):
            ctx = safe_change_context(
                str(project_root), target=target_rel, action="add_parameter",
                function="helper", param_name="verbose",
            )
        assert ctx["metrics"]["is_safe"] is True

    def test_is_safe_false_on_edit_error(self, project_root, target_rel):
        """is_safe False when transform fails."""
        with patch("odibi_anchor.codebase.semantic_edit_context._HAS_LIBCST", False):
            ctx = safe_change_context(
                str(project_root), target=target_rel, action="remove_parameter",
                function="helper", param_name="x",
            )
        # ast fallback doesn't support remove_parameter → transform error → not safe
        assert ctx["metrics"]["is_safe"] is False

    def test_apply_writes_file(self, project_root, target_rel):
        """apply=True writes the change."""
        with patch("odibi_anchor.codebase.semantic_edit_context._HAS_LIBCST", False):
            ctx = safe_change_context(
                str(project_root), target=target_rel, action="add_parameter",
                function="helper", param_name="verbose", apply=True,
            )
        assert ctx["metrics"]["applied"] is True
        new_source = (project_root / target_rel).read_text()
        assert "verbose" in new_source


# ---------------------------------------------------------------------------
# Manifest Constraint Enforcement (Step 1c)
# ---------------------------------------------------------------------------


class TestManifestConstraints:
    """Tests for forbidden_patterns enforcement from .anchor_manifest.json."""

    @pytest.fixture
    def manifest_root(self, tmp_path):
        """Project root with a target file and a manifest with forbidden_patterns."""
        src = tmp_path / "src" / "mylib"
        src.mkdir(parents=True)
        target = src / "utils.py"
        target.write_text(
            'import os\n\n\ndef helper(x: int, y: int) -> int:\n'
            '    """Add two numbers."""\n    return x + y\n'
        )
        manifest = {
            "project": {"name": "test-project"},
            "constraints": {
                "forbidden_patterns": ["from legacy_pkg", "import *", "spark.read"]
            }
        }
        (tmp_path / ".anchor_manifest.json").write_text(json.dumps(manifest))
        return tmp_path

    def test_violation_detected_in_added_lines(self, manifest_root):
        """Forbidden pattern in diff should produce manifest_violations > 0."""
        with patch("odibi_anchor.codebase.semantic_edit_context._HAS_LIBCST", False):
            # add_parameter adds "from legacy_pkg" nowhere, so we mock the diff
            # Instead, test directly by creating a file that will have a forbidden import added
            target = manifest_root / "src" / "mylib" / "utils.py"
            target.write_text(
                'def process():\n    pass\n'
            )
            # We'll patch semantic_edit_context to return a diff with forbidden content
            fake_edit = {
                "metrics": {"has_changes": True, "applied": False},
                "diff_preview": (
                    "--- a/src/mylib/utils.py\n"
                    "+++ b/src/mylib/utils.py\n"
                    "@@ -1,2 +1,3 @@\n"
                    "+from legacy_pkg import something\n"
                    " def process():\n"
                    "     pass\n"
                ),
                "risks": [],
                "summary": "added import",
            }
            with patch(
                "odibi_anchor.codebase.semantic_edit_context.semantic_edit_context",
                return_value=fake_edit,
            ):
                ctx = safe_change_context(
                    str(manifest_root), target="src/mylib/utils.py",
                    action="add_import", guardrail=False, verify=False, test=False,
                )
        assert ctx["metrics"]["manifest_violations"] == 1
        assert any("Manifest:" in f for f in ctx["findings"])
        assert any("MANIFEST CONSTRAINT:" in r for r in ctx["risks"])

    def test_no_violation_when_pattern_not_in_added_lines(self, manifest_root):
        """Clean diff should produce manifest_violations == 0."""
        with patch("odibi_anchor.codebase.semantic_edit_context._HAS_LIBCST", False):
            fake_edit = {
                "metrics": {"has_changes": True, "applied": False},
                "diff_preview": (
                    "--- a/src/mylib/utils.py\n"
                    "+++ b/src/mylib/utils.py\n"
                    "@@ -1,2 +1,3 @@\n"
                    "+import json\n"
                    " def process():\n"
                    "     pass\n"
                ),
                "risks": [],
                "summary": "added import",
            }
            with patch(
                "odibi_anchor.codebase.semantic_edit_context.semantic_edit_context",
                return_value=fake_edit,
            ):
                ctx = safe_change_context(
                    str(manifest_root), target="src/mylib/utils.py",
                    action="add_import", guardrail=False, verify=False, test=False,
                )
        assert ctx["metrics"]["manifest_violations"] == 0
        assert not any("Manifest:" in f for f in ctx["findings"])

    def test_context_lines_not_checked(self, manifest_root):
        """Forbidden pattern in context lines (no +) should NOT trigger violation."""
        with patch("odibi_anchor.codebase.semantic_edit_context._HAS_LIBCST", False):
            fake_edit = {
                "metrics": {"has_changes": True, "applied": False},
                "diff_preview": (
                    "--- a/src/mylib/utils.py\n"
                    "+++ b/src/mylib/utils.py\n"
                    "@@ -1,3 +1,4 @@\n"
                    " from legacy_pkg import old_thing\n"  # context line — not new
                    "+import json\n"  # added line — clean
                    " def process():\n"
                    "     pass\n"
                ),
                "risks": [],
                "summary": "added import",
            }
            with patch(
                "odibi_anchor.codebase.semantic_edit_context.semantic_edit_context",
                return_value=fake_edit,
            ):
                ctx = safe_change_context(
                    str(manifest_root), target="src/mylib/utils.py",
                    action="add_import", guardrail=False, verify=False, test=False,
                )
        # "from legacy_pkg" is in context line, not added — should NOT match
        assert ctx["metrics"]["manifest_violations"] == 0

    def test_no_manifest_file_skips_check(self, tmp_path):
        """Missing .anchor_manifest.json should not break — violations == 0."""
        src = tmp_path / "src" / "mylib"
        src.mkdir(parents=True)
        (src / "utils.py").write_text('def process():\n    pass\n')
        with patch("odibi_anchor.codebase.semantic_edit_context._HAS_LIBCST", False):
            fake_edit = {
                "metrics": {"has_changes": True, "applied": False},
                "diff_preview": (
                    "--- a/src/mylib/utils.py\n"
                    "+++ b/src/mylib/utils.py\n"
                    "@@ -1,2 +1,3 @@\n"
                    "+from legacy_pkg import bad\n"
                    " def process():\n"
                    "     pass\n"
                ),
                "risks": [],
                "summary": "added import",
            }
            with patch(
                "odibi_anchor.codebase.semantic_edit_context.semantic_edit_context",
                return_value=fake_edit,
            ):
                ctx = safe_change_context(
                    str(tmp_path), target="src/mylib/utils.py",
                    action="add_import", guardrail=False, verify=False, test=False,
                )
        # No manifest → no violations (graceful skip)
        assert ctx["metrics"]["manifest_violations"] == 0

    def test_empty_forbidden_patterns_skips_check(self, tmp_path):
        """Manifest with empty forbidden_patterns should not flag anything."""
        src = tmp_path / "src" / "mylib"
        src.mkdir(parents=True)
        (src / "utils.py").write_text('def process():\n    pass\n')
        manifest = {
            "project": {"name": "test-project"},
            "constraints": {"forbidden_patterns": []}
        }
        (tmp_path / ".anchor_manifest.json").write_text(json.dumps(manifest))
        with patch("odibi_anchor.codebase.semantic_edit_context._HAS_LIBCST", False):
            fake_edit = {
                "metrics": {"has_changes": True, "applied": False},
                "diff_preview": (
                    "--- a/src/mylib/utils.py\n"
                    "+++ b/src/mylib/utils.py\n"
                    "@@ -1,2 +1,3 @@\n"
                    "+from legacy_pkg import bad\n"
                    " def process():\n"
                    "     pass\n"
                ),
                "risks": [],
                "summary": "added import",
            }
            with patch(
                "odibi_anchor.codebase.semantic_edit_context.semantic_edit_context",
                return_value=fake_edit,
            ):
                ctx = safe_change_context(
                    str(tmp_path), target="src/mylib/utils.py",
                    action="add_import", guardrail=False, verify=False, test=False,
                )
        assert ctx["metrics"]["manifest_violations"] == 0

    def test_multiple_violations_counted(self, manifest_root):
        """Multiple forbidden patterns in one diff should all be reported."""
        with patch("odibi_anchor.codebase.semantic_edit_context._HAS_LIBCST", False):
            fake_edit = {
                "metrics": {"has_changes": True, "applied": False},
                "diff_preview": (
                    "--- a/src/mylib/utils.py\n"
                    "+++ b/src/mylib/utils.py\n"
                    "@@ -1,2 +1,4 @@\n"
                    "+from legacy_pkg import something\n"
                    "+import *\n"
                    "+df = spark.read.table('x')\n"
                    " def process():\n"
                    "     pass\n"
                ),
                "risks": [],
                "summary": "added imports",
            }
            with patch(
                "odibi_anchor.codebase.semantic_edit_context.semantic_edit_context",
                return_value=fake_edit,
            ):
                ctx = safe_change_context(
                    str(manifest_root), target="src/mylib/utils.py",
                    action="add_import", guardrail=False, verify=False, test=False,
                )
        # All 3 patterns should match: "from legacy_pkg", "import *", "spark.read"
        assert ctx["metrics"]["manifest_violations"] == 3
        manifest_findings = [f for f in ctx["findings"] if f.startswith("Manifest:")]
        assert len(manifest_findings) == 3


# ---------------------------------------------------------------------------
# Sensitive Columns Enforcement (Step 1c-ii)
# ---------------------------------------------------------------------------


class TestSensitiveColumns:
    """Tests for sensitive_columns regex enforcement from .anchor_manifest.json."""

    @pytest.fixture
    def sensitive_root(self, tmp_path):
        """Project root with manifest containing sensitive_columns patterns."""
        src = tmp_path / "src" / "mylib"
        src.mkdir(parents=True)
        (src / "utils.py").write_text('def process():\n    pass\n')
        manifest = {
            "project": {"name": "test-project"},
            "constraints": {
                "sensitive_columns": [".*email.*", ".*ssn.*", ".*phone.*"],
                "forbidden_patterns": []
            }
        }
        (tmp_path / ".anchor_manifest.json").write_text(json.dumps(manifest))
        return tmp_path

    def test_sensitive_column_detected(self, sensitive_root):
        """String literal matching sensitive pattern should produce warning."""
        fake_edit = {
            "metrics": {"has_changes": True, "applied": False},
            "diff_preview": (
                "--- a/src/mylib/utils.py\n"
                "+++ b/src/mylib/utils.py\n"
                "@@ -1,2 +1,3 @@\n"
                "+    result = df[\"user_email\"]\n"
                " def process():\n"
                "     pass\n"
            ),
            "risks": [],
            "summary": "added column ref",
        }
        with patch(
            "odibi_anchor.codebase.semantic_edit_context.semantic_edit_context",
            return_value=fake_edit,
        ):
            ctx = safe_change_context(
                str(sensitive_root), target="src/mylib/utils.py",
                action="add_import", guardrail=False, verify=False, test=False,
            )
        assert ctx["metrics"]["sensitive_column_refs"] >= 1
        assert any("Sensitive:" in f for f in ctx["findings"])
        assert any("PII RISK:" in r for r in ctx["risks"])

    def test_no_sensitive_match_on_clean_code(self, sensitive_root):
        """Code without sensitive column refs should produce 0 warnings."""
        fake_edit = {
            "metrics": {"has_changes": True, "applied": False},
            "diff_preview": (
                "--- a/src/mylib/utils.py\n"
                "+++ b/src/mylib/utils.py\n"
                "@@ -1,2 +1,3 @@\n"
                "+    result = df[\"total_count\"]\n"
                " def process():\n"
                "     pass\n"
            ),
            "risks": [],
            "summary": "added column ref",
        }
        with patch(
            "odibi_anchor.codebase.semantic_edit_context.semantic_edit_context",
            return_value=fake_edit,
        ):
            ctx = safe_change_context(
                str(sensitive_root), target="src/mylib/utils.py",
                action="add_import", guardrail=False, verify=False, test=False,
            )
        assert ctx["metrics"]["sensitive_column_refs"] == 0
        assert not any("Sensitive:" in f for f in ctx["findings"])

    def test_multiple_sensitive_patterns_matched(self, sensitive_root):
        """Multiple sensitive patterns in one diff should all be reported."""
        fake_edit = {
            "metrics": {"has_changes": True, "applied": False},
            "diff_preview": (
                "--- a/src/mylib/utils.py\n"
                "+++ b/src/mylib/utils.py\n"
                "@@ -1,2 +1,4 @@\n"
                "+    email_col = col(\'user_email\')\n"
                "+    phone_col = col(\'phone_number\')\n"
                " def process():\n"
                "     pass\n"
            ),
            "risks": [],
            "summary": "added columns",
        }
        with patch(
            "odibi_anchor.codebase.semantic_edit_context.semantic_edit_context",
            return_value=fake_edit,
        ):
            ctx = safe_change_context(
                str(sensitive_root), target="src/mylib/utils.py",
                action="add_import", guardrail=False, verify=False, test=False,
            )
        # Should match both user_email (.*email.*) and phone_number (.*phone.*)
        assert ctx["metrics"]["sensitive_column_refs"] >= 2

    def test_invalid_regex_skipped_gracefully(self, tmp_path):
        """Invalid regex in sensitive_columns should be skipped, not crash."""
        src = tmp_path / "src" / "mylib"
        src.mkdir(parents=True)
        (src / "utils.py").write_text('def process():\n    pass\n')
        manifest = {
            "project": {"name": "test-project"},
            "constraints": {
                "sensitive_columns": ["[invalid(regex", ".*email.*"],
                "forbidden_patterns": []
            }
        }
        (tmp_path / ".anchor_manifest.json").write_text(json.dumps(manifest))

        fake_edit = {
            "metrics": {"has_changes": True, "applied": False},
            "diff_preview": (
                "--- a/src/mylib/utils.py\n"
                "+++ b/src/mylib/utils.py\n"
                "@@ -1,2 +1,3 @@\n"
                "+    x = col(\'user_email\')\n"
                " def process():\n"
                "     pass\n"
            ),
            "risks": [],
            "summary": "added ref",
        }
        with patch(
            "odibi_anchor.codebase.semantic_edit_context.semantic_edit_context",
            return_value=fake_edit,
        ):
            ctx = safe_change_context(
                str(tmp_path), target="src/mylib/utils.py",
                action="add_import", guardrail=False, verify=False, test=False,
            )
        # Invalid regex skipped, but valid one still catches user_email
        assert ctx["metrics"]["sensitive_column_refs"] >= 1

    def test_empty_sensitive_columns_skips_check(self, tmp_path):
        """Empty sensitive_columns list should not produce any warnings."""
        src = tmp_path / "src" / "mylib"
        src.mkdir(parents=True)
        (src / "utils.py").write_text('def process():\n    pass\n')
        manifest = {
            "project": {"name": "test-project"},
            "constraints": {
                "sensitive_columns": [],
                "forbidden_patterns": []
            }
        }
        (tmp_path / ".anchor_manifest.json").write_text(json.dumps(manifest))

        fake_edit = {
            "metrics": {"has_changes": True, "applied": False},
            "diff_preview": (
                "--- a/src/mylib/utils.py\n"
                "+++ b/src/mylib/utils.py\n"
                "@@ -1,2 +1,3 @@\n"
                "+    x = col(\'user_email\')\n"
                " def process():\n"
                "     pass\n"
            ),
            "risks": [],
            "summary": "added ref",
        }
        with patch(
            "odibi_anchor.codebase.semantic_edit_context.semantic_edit_context",
            return_value=fake_edit,
        ):
            ctx = safe_change_context(
                str(tmp_path), target="src/mylib/utils.py",
                action="add_import", guardrail=False, verify=False, test=False,
            )
        assert ctx["metrics"]["sensitive_column_refs"] == 0

    def test_context_lines_not_checked_for_sensitive(self, sensitive_root):
        """Sensitive column in context lines (no +) should NOT trigger."""
        fake_edit = {
            "metrics": {"has_changes": True, "applied": False},
            "diff_preview": (
                "--- a/src/mylib/utils.py\n"
                "+++ b/src/mylib/utils.py\n"
                "@@ -1,3 +1,4 @@\n"
                " existing = col(\'user_email\')\n"  # context line
                "+    x = col(\'total_count\')\n"  # added — clean
                " def process():\n"
                "     pass\n"
            ),
            "risks": [],
            "summary": "added ref",
        }
        with patch(
            "odibi_anchor.codebase.semantic_edit_context.semantic_edit_context",
            return_value=fake_edit,
        ):
            ctx = safe_change_context(
                str(sensitive_root), target="src/mylib/utils.py",
                action="add_import", guardrail=False, verify=False, test=False,
            )
        # "user_email" is in a context line, not an added line
        assert ctx["metrics"]["sensitive_column_refs"] == 0


# ---------------------------------------------------------------------------
# Read-Only Catalogs Enforcement (Step 1c-iii)
# ---------------------------------------------------------------------------


class TestReadOnlyCatalogs:
    """Tests for read_only_catalogs write-detection from .anchor_manifest.json."""

    @pytest.fixture
    def ro_root(self, tmp_path):
        """Project root with manifest containing read_only_catalogs."""
        src = tmp_path / "src" / "mylib"
        src.mkdir(parents=True)
        (src / "utils.py").write_text('def process():\n    pass\n')
        manifest = {
            "project": {"name": "test-project"},
            "constraints": {
                "read_only_catalogs": ["eaai_prod", "system"],
                "forbidden_patterns": [],
                "sensitive_columns": []
            }
        }
        (tmp_path / ".anchor_manifest.json").write_text(json.dumps(manifest))
        return tmp_path

    def test_write_to_read_only_catalog_detected(self, ro_root):
        """Writing to a read-only catalog should produce violation."""
        fake_edit = {
            "metrics": {"has_changes": True, "applied": False},
            "diff_preview": (
                "--- a/src/mylib/utils.py\n"
                "+++ b/src/mylib/utils.py\n"
                "@@ -1,2 +1,3 @@\n"
                "+    df.write.saveAsTable('eaai_prod.schema.table')\n"
                " def process():\n"
                "     pass\n"
            ),
            "risks": [],
            "summary": "added write",
        }
        with patch(
            "odibi_anchor.codebase.semantic_edit_context.semantic_edit_context",
            return_value=fake_edit,
        ):
            ctx = safe_change_context(
                str(ro_root), target="src/mylib/utils.py",
                action="add_import", guardrail=False, verify=False, test=False,
            )
        assert ctx["metrics"]["manifest_violations"] >= 1
        assert any("read-only catalog" in f for f in ctx["findings"])

    def test_read_from_read_only_catalog_ok(self, ro_root):
        """Reading from a read-only catalog should NOT produce violation."""
        fake_edit = {
            "metrics": {"has_changes": True, "applied": False},
            "diff_preview": (
                "--- a/src/mylib/utils.py\n"
                "+++ b/src/mylib/utils.py\n"
                "@@ -1,2 +1,3 @@\n"
                "+    df = spark.table('eaai_prod.schema.table')\n"
                " def process():\n"
                "     pass\n"
            ),
            "risks": [],
            "summary": "added read",
        }
        with patch(
            "odibi_anchor.codebase.semantic_edit_context.semantic_edit_context",
            return_value=fake_edit,
        ):
            ctx = safe_change_context(
                str(ro_root), target="src/mylib/utils.py",
                action="add_import", guardrail=False, verify=False, test=False,
            )
        # No write patterns → no violation despite catalog name being present
        assert not any("read-only catalog" in f for f in ctx["findings"])

    def test_no_violation_when_catalog_not_in_list(self, ro_root):
        """Writing to an allowed catalog should not produce violation."""
        fake_edit = {
            "metrics": {"has_changes": True, "applied": False},
            "diff_preview": (
                "--- a/src/mylib/utils.py\n"
                "+++ b/src/mylib/utils.py\n"
                "@@ -1,2 +1,3 @@\n"
                "+    df.write.saveAsTable('analytics_dev.schema.table')\n"
                " def process():\n"
                "     pass\n"
            ),
            "risks": [],
            "summary": "added write",
        }
        with patch(
            "odibi_anchor.codebase.semantic_edit_context.semantic_edit_context",
            return_value=fake_edit,
        ):
            ctx = safe_change_context(
                str(ro_root), target="src/mylib/utils.py",
                action="add_import", guardrail=False, verify=False, test=False,
            )
        assert not any("read-only catalog" in f for f in ctx["findings"])


# ---------------------------------------------------------------------------
# Max Table Rows Local (Step 1c-iv)
# ---------------------------------------------------------------------------


class TestMaxTableRowsLocal:
    """Tests for max_table_rows_local local-processing warnings."""

    @pytest.fixture
    def local_root(self, tmp_path):
        """Project root with manifest containing max_table_rows_local."""
        src = tmp_path / "src" / "mylib"
        src.mkdir(parents=True)
        (src / "utils.py").write_text('def process():\n    pass\n')
        manifest = {
            "project": {"name": "test-project"},
            "constraints": {
                "max_table_rows_local": 500000,
                "forbidden_patterns": [],
                "sensitive_columns": [],
                "read_only_catalogs": []
            }
        }
        (tmp_path / ".anchor_manifest.json").write_text(json.dumps(manifest))
        return tmp_path

    def test_topandas_triggers_warning(self, local_root):
        """.toPandas() in added code should produce local processing warning."""
        fake_edit = {
            "metrics": {"has_changes": True, "applied": False},
            "diff_preview": (
                "--- a/src/mylib/utils.py\n"
                "+++ b/src/mylib/utils.py\n"
                "@@ -1,2 +1,3 @@\n"
                "+    pdf = df.toPandas()\n"
                " def process():\n"
                "     pass\n"
            ),
            "risks": [],
            "summary": "added local processing",
        }
        with patch(
            "odibi_anchor.codebase.semantic_edit_context.semantic_edit_context",
            return_value=fake_edit,
        ):
            ctx = safe_change_context(
                str(local_root), target="src/mylib/utils.py",
                action="add_import", guardrail=False, verify=False, test=False,
            )
        assert ctx["metrics"]["manifest_violations"] >= 1
        assert any("500,000 rows" in f for f in ctx["findings"])

    def test_collect_triggers_warning(self, local_root):
        """.collect() in added code should produce local processing warning."""
        fake_edit = {
            "metrics": {"has_changes": True, "applied": False},
            "diff_preview": (
                "--- a/src/mylib/utils.py\n"
                "+++ b/src/mylib/utils.py\n"
                "@@ -1,2 +1,3 @@\n"
                "+    rows = df.collect()\n"
                " def process():\n"
                "     pass\n"
            ),
            "risks": [],
            "summary": "added collect",
        }
        with patch(
            "odibi_anchor.codebase.semantic_edit_context.semantic_edit_context",
            return_value=fake_edit,
        ):
            ctx = safe_change_context(
                str(local_root), target="src/mylib/utils.py",
                action="add_import", guardrail=False, verify=False, test=False,
            )
        assert ctx["metrics"]["manifest_violations"] >= 1

    def test_no_warning_without_local_patterns(self, local_root):
        """Code without local processing patterns should not trigger."""
        fake_edit = {
            "metrics": {"has_changes": True, "applied": False},
            "diff_preview": (
                "--- a/src/mylib/utils.py\n"
                "+++ b/src/mylib/utils.py\n"
                "@@ -1,2 +1,3 @@\n"
                "+    df = df.filter(col('x') > 0)\n"
                " def process():\n"
                "     pass\n"
            ),
            "risks": [],
            "summary": "added filter",
        }
        with patch(
            "odibi_anchor.codebase.semantic_edit_context.semantic_edit_context",
            return_value=fake_edit,
        ):
            ctx = safe_change_context(
                str(local_root), target="src/mylib/utils.py",
                action="add_import", guardrail=False, verify=False, test=False,
            )
        # No .toPandas()/.collect() → no violation from this check
        assert not any("rows" in f and "manifest limit" in f for f in ctx["findings"])
