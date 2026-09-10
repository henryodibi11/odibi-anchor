"""Tests for odibi_anchor.codebase._manifest module."""

from __future__ import annotations

import json
import pytest
from pathlib import Path
from unittest.mock import patch

from odibi_anchor.codebase._manifest import (
    validate_manifest,
    load_manifest,
    generate_manifest,
    manifest_context,
    render_manifest_report,
    VALID_PROJECT_TYPES,
    VALID_REFRESH_SCHEDULES,
    VALID_ACCESS_MODES,
    _get_unknown_keys,
)


# ── validate_manifest tests ──────────────────────────────────────────────────


class TestValidateManifest:
    """Tests for validate_manifest()."""

    def test_valid_minimal(self):
        """Minimal valid manifest with just project.name."""
        data = {"project": {"name": "test-project"}}
        assert validate_manifest(data) == []

    def test_valid_full(self):
        """Full valid manifest with all sections."""
        data = {
            "project": {"name": "test", "type": "pipeline"},
            "data_sources": [
                {"name": "src", "refresh_schedule": "daily", "access": "read"}
            ],
            "constraints": {
                "max_table_rows_local": 100000,
                "sensitive_columns": [".*email.*"],
            },
            "environments": {
                "dev": {"catalog": "dev_catalog"},
                "prod": {"catalog": "prod_catalog", "read_only": True},
            },
        }
        assert validate_manifest(data) == []

    def test_rule1_root_not_dict(self):
        """Rule 1: Root must be a dict."""
        errors = validate_manifest([1, 2, 3])
        assert "Root must be a JSON object (dict)." in errors

    def test_rule2_project_name_required(self):
        """Rule 2: project.name is required."""
        errors = validate_manifest({"project": {"name": ""}})
        assert any("project.name" in e for e in errors)

    def test_rule2_project_name_missing(self):
        """Rule 2: project.name missing entirely."""
        errors = validate_manifest({"project": {}})
        assert any("project.name" in e for e in errors)

    def test_rule3_project_type_enum(self):
        """Rule 3: project.type must be valid enum."""
        errors = validate_manifest({"project": {"name": "x", "type": "invalid"}})
        assert any("project.type" in e for e in errors)

    def test_rule3_project_type_valid(self):
        """Rule 3: All valid project types accepted."""
        for ptype in VALID_PROJECT_TYPES:
            errors = validate_manifest({"project": {"name": "x", "type": ptype}})
            assert not any("project.type" in e for e in errors)

    def test_rule4_data_sources_must_be_array(self):
        """Rule 4: data_sources must be an array."""
        errors = validate_manifest({"data_sources": "not_a_list"})
        assert any("must be an array" in e for e in errors)

    def test_rule4_data_sources_entries_need_name(self):
        """Rule 4: Each data_source entry needs a name."""
        errors = validate_manifest({"data_sources": [{"catalog": "x"}]})
        assert any("name" in e and "required" in e for e in errors)

    def test_rule5_refresh_schedule_enum(self):
        """Rule 5: refresh_schedule must be valid enum."""
        errors = validate_manifest({
            "data_sources": [{"name": "x", "refresh_schedule": "weekly"}]
        })
        assert any("refresh_schedule" in e for e in errors)

    def test_rule6_access_enum(self):
        """Rule 6: access must be valid enum."""
        errors = validate_manifest({
            "data_sources": [{"name": "x", "access": "admin"}]
        })
        assert any("access" in e for e in errors)

    def test_rule7_sensitive_columns_valid_regex(self):
        """Rule 7: sensitive_columns must be valid regexes."""
        errors = validate_manifest({
            "constraints": {"sensitive_columns": ["[invalid"]}
        })
        assert any("not a valid regex" in e for e in errors)

    def test_rule7_sensitive_columns_valid(self):
        """Rule 7: Valid regexes pass."""
        errors = validate_manifest({
            "constraints": {"sensitive_columns": [".*email.*", "^phone"]}
        })
        assert not any("regex" in e for e in errors)

    def test_rule8_max_rows_positive(self):
        """Rule 8: max_table_rows_local must be positive."""
        errors = validate_manifest({
            "constraints": {"max_table_rows_local": -1}
        })
        assert any("positive integer" in e for e in errors)

    def test_rule8_max_rows_zero(self):
        """Rule 8: max_table_rows_local cannot be zero."""
        errors = validate_manifest({
            "constraints": {"max_table_rows_local": 0}
        })
        assert any("positive integer" in e for e in errors)

    def test_rule9_environments_must_be_objects(self):
        """Rule 9: Environment values must be objects."""
        errors = validate_manifest({"environments": {"dev": "not_a_dict"}})
        assert any("must be an object" in e for e in errors)

    def test_rule9_read_only_must_be_boolean(self):
        """Rule 9: read_only must be boolean."""
        errors = validate_manifest({
            "environments": {"dev": {"read_only": "yes"}}
        })
        assert any("must be a boolean" in e for e in errors)

    def test_empty_dict_valid(self):
        """Empty dict is valid (no required top-level fields)."""
        assert validate_manifest({}) == []


class TestGetUnknownKeys:
    """Tests for _get_unknown_keys()."""

    def test_no_unknown(self):
        assert _get_unknown_keys({"project": {}, "tools": []}) == []

    def test_unknown_detected(self):
        result = _get_unknown_keys({"project": {}, "foo": "bar", "baz": 1})
        assert "foo" in result
        assert "baz" in result


# ── load_manifest tests ──────────────────────────────────────────────────────


class TestLoadManifest:
    """Tests for load_manifest()."""

    def test_missing_file(self, tmp_path):
        """Returns {} when no manifest exists."""
        assert load_manifest(tmp_path) == {}

    def test_valid_manifest(self, tmp_path):
        """Loads and returns valid manifest."""
        data = {"project": {"name": "test"}}
        (tmp_path / ".anchor_manifest.json").write_text(json.dumps(data))
        result = load_manifest(tmp_path)
        assert result["project"]["name"] == "test"

    def test_invalid_json(self, tmp_path):
        """Returns {} on invalid JSON."""
        (tmp_path / ".anchor_manifest.json").write_text("{invalid json")
        assert load_manifest(tmp_path) == {}

    def test_non_dict_json(self, tmp_path):
        """Returns {} if JSON is not a dict."""
        (tmp_path / ".anchor_manifest.json").write_text("[1, 2, 3]")
        assert load_manifest(tmp_path) == {}

    def test_validation_errors_attached(self, tmp_path):
        """Attaches validation errors but still returns data."""
        data = {"project": {"name": "", "type": "invalid"}}
        (tmp_path / ".anchor_manifest.json").write_text(json.dumps(data))
        result = load_manifest(tmp_path)
        assert "_validation_errors" in result
        assert len(result["_validation_errors"]) > 0


# ── generate_manifest tests ──────────────────────────────────────────────────


class TestGenerateManifest:
    """Tests for generate_manifest()."""

    def test_empty_directory(self, tmp_path):
        """Returns minimal manifest for empty directory."""
        result = generate_manifest(tmp_path)
        assert result["_generated"] is True
        assert result["project"]["name"] == tmp_path.name

    def test_detects_python(self, tmp_path):
        """Detects Python language from .py files."""
        (tmp_path / "main.py").write_text("print('hello')")
        result = generate_manifest(tmp_path)
        assert result["project"]["language"] == "python"

    def test_detects_tests_dir(self, tmp_path):
        """Detects pytest convention from tests/ directory."""
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_foo.py").write_text("def test_x(): pass")
        result = generate_manifest(tmp_path)
        assert "testing" in result.get("conventions", {})

    def test_reads_requirements(self, tmp_path):
        """Reads dependencies from requirements.txt."""
        (tmp_path / "requirements.txt").write_text("pandas>=2.0\nnumpy==1.24\n")
        result = generate_manifest(tmp_path)
        deps = result.get("dependencies", {}).get("external", {})
        assert "pandas" in deps
        assert "numpy" in deps

    def test_reads_pyproject_name(self, tmp_path):
        """Reads project name from pyproject.toml."""
        toml = '''[project]\nname = "my-cool-project"\ndescription = "A test"\n'''
        (tmp_path / "pyproject.toml").write_text(toml)
        result = generate_manifest(tmp_path)
        assert result["project"]["name"] == "my-cool-project"
        assert result["project"]["description"] == "A test"

    def test_detects_scripts(self, tmp_path):
        """Detects tools from scripts/ directory."""
        scripts = tmp_path / "scripts"
        scripts.mkdir()
        (scripts / "run_pipeline.py").write_text("# script")
        (scripts / "_internal.py").write_text("# skip")
        result = generate_manifest(tmp_path)
        tools = result.get("tools", [])
        assert any(t["name"] == "run_pipeline" for t in tools)
        assert not any(t["name"] == "_internal" for t in tools)


# ── manifest_context tests ───────────────────────────────────────────────────


class TestManifestContext:
    """Tests for manifest_context()."""

    def test_no_manifest(self, tmp_path):
        """Returns standard contract with has_manifest=False."""
        ctx = manifest_context(tmp_path, output_format="dict")
        assert ctx["kind"] == "manifest"
        assert ctx["metrics"]["has_manifest"] is False

    def test_generate_mode(self, tmp_path):
        """Generate mode returns valid context."""
        (tmp_path / "main.py").write_text("x = 1")
        ctx = manifest_context(tmp_path, generate=True, output_format="dict")
        assert ctx["metrics"]["has_manifest"] is True
        assert ctx["metrics"]["is_generated"] is True

    def test_markdown_output(self, tmp_path):
        """Markdown output is a string with headers."""
        (tmp_path / "main.py").write_text("x = 1")
        md = manifest_context(tmp_path, generate=True, output_format="markdown")
        assert isinstance(md, str)
        assert "# Manifest:" in md

    def test_section_filter(self, tmp_path):
        """Section filter shows only requested section."""
        data = {
            "project": {"name": "test"},
            "constraints": {"max_table_rows_local": 1000},
        }
        (tmp_path / ".anchor_manifest.json").write_text(json.dumps(data))
        ctx = manifest_context(tmp_path, section="constraints", output_format="dict")
        manifest_data = ctx["samples"]["manifest"]
        assert "constraints" in manifest_data
        assert "project" not in manifest_data
