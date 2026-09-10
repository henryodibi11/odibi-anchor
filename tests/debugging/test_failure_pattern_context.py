"""Tests for failure_pattern_context."""

import json
import pytest

from odibi_anchor.debugging.failure_pattern_context import (
    failure_pattern_context,
    render_failure_pattern_report,
    _load_patterns_file,
    _parse_simple_yaml,
    _match_patterns,
    _normalize_pattern,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


SAMPLE_PATTERNS = [
    {
        "pattern": "fixture .* not found",
        "context": "module with test_ prefix imported into test file",
        "root_cause": "pytest collects imported modules named test_*",
        "fix": "rename source file OR alias import with underscore prefix",
        "never_try": ["collect_ignore_glob", "conftest hooks for imported modules"],
    },
    {
        "pattern": "ContractViolation",
        "context": "tool output has wrong types",
        "root_cause": "build_base_context enforces runtime types",
        "fix": "check the exact key type",
        "never_try": ["wrapping in try/except"],
    },
    {
        "pattern": "UNRESOLVED_COLUMN",
        "context": "Spark SQL column reference",
        "root_cause": "column name mismatch",
        "fix": "run table_contract_summary to get actual columns",
        "never_try": ["guessing column names"],
    },
]


# ---------------------------------------------------------------------------
# Standard Contract Tests
# ---------------------------------------------------------------------------


class TestStandardContract:
    def test_returns_dict_by_default(self):
        ctx = failure_pattern_context("some error", patterns=SAMPLE_PATTERNS)
        assert isinstance(ctx, dict)

    def test_has_all_standard_fields(self):
        ctx = failure_pattern_context("some error", patterns=SAMPLE_PATTERNS)
        required = {"kind", "subject", "summary", "metrics", "findings",
                     "risks", "samples", "suggested_next_actions"}
        assert required.issubset(ctx.keys())

    def test_kind_is_correct(self):
        ctx = failure_pattern_context("some error", patterns=SAMPLE_PATTERNS)
        assert ctx["kind"] == "failure_pattern_context"

    def test_json_serializable(self):
        ctx = failure_pattern_context(
            "fixture 'root' not found",
            patterns=SAMPLE_PATTERNS,
        )
        json.dumps(ctx)  # Should not raise

    def test_markdown_output(self):
        md = failure_pattern_context(
            "fixture 'root' not found",
            patterns=SAMPLE_PATTERNS,
            output_format="markdown",
        )
        assert isinstance(md, str)
        assert "Failure Pattern Lookup" in md

    def test_invalid_output_format_raises(self):
        with pytest.raises(ValueError, match="output_format"):
            failure_pattern_context("error", patterns=[], output_format="xml")


# ---------------------------------------------------------------------------
# Matching Tests
# ---------------------------------------------------------------------------


class TestMatching:
    def test_regex_match(self):
        ctx = failure_pattern_context(
            "fixture 'root' not found",
            patterns=SAMPLE_PATTERNS,
        )
        assert ctx["metrics"]["has_match"] is True
        assert len(ctx["matched_patterns"]) >= 1
        assert "pytest collects" in ctx["matched_patterns"][0]["root_cause"]

    def test_substring_match(self):
        ctx = failure_pattern_context(
            "Got a ContractViolation error",
            patterns=SAMPLE_PATTERNS,
        )
        assert ctx["metrics"]["has_match"] is True
        assert "ContractViolation" in ctx["matched_patterns"][0]["pattern"]

    def test_no_match(self):
        ctx = failure_pattern_context(
            "totally unrelated error about network timeout",
            patterns=SAMPLE_PATTERNS,
        )
        assert ctx["metrics"]["has_match"] is False
        assert ctx["matched_patterns"] == []

    def test_never_try_in_samples(self):
        ctx = failure_pattern_context(
            "fixture 'root' not found",
            patterns=SAMPLE_PATTERNS,
        )
        assert "never_try" in ctx["samples"]
        assert "collect_ignore_glob" in ctx["samples"]["never_try"]

    def test_never_try_in_risks(self):
        ctx = failure_pattern_context(
            "fixture 'root' not found",
            patterns=SAMPLE_PATTERNS,
        )
        assert any("do NOT try" in r for r in ctx["risks"])

    def test_max_matches_respected(self):
        # All three patterns have "not" somewhere, use a broad error
        many_patterns = SAMPLE_PATTERNS * 5  # 15 patterns
        ctx = failure_pattern_context(
            "fixture 'root' not found ContractViolation UNRESOLVED_COLUMN",
            patterns=many_patterns,
            max_matches=2,
        )
        assert len(ctx["matched_patterns"]) <= 2

    def test_empty_patterns(self):
        ctx = failure_pattern_context("some error", patterns=[])
        assert ctx["metrics"]["patterns_matched"] == 0
        assert ctx["metrics"]["has_match"] is False

    def test_no_patterns_source(self):
        """No patterns_path or patterns list — empty match."""
        ctx = failure_pattern_context("some error")
        assert ctx["metrics"]["patterns_searched"] == 0
        assert ctx["metrics"]["has_match"] is False


# ---------------------------------------------------------------------------
# File Loading Tests
# ---------------------------------------------------------------------------


class TestFileLoading:
    def test_load_nonexistent_file(self, tmp_path):
        result = _load_patterns_file(tmp_path / "nonexistent.yaml")
        assert result == []

    def test_load_from_file(self, tmp_path):
        yaml_content = '''- pattern: "test error"
  context: "testing"
  root_cause: "test root cause"
  fix: "test fix"
  never_try: ["approach1", "approach2"]
'''
        f = tmp_path / "patterns.yaml"
        f.write_text(yaml_content)
        result = _load_patterns_file(f)
        assert len(result) >= 1
        assert result[0]["pattern"] == "test error"
        assert result[0]["never_try"] == ["approach1", "approach2"]

    def test_context_from_file(self, tmp_path):
        yaml_content = '''- pattern: "KeyError"
  context: "accessing dict or DataFrame"
  root_cause: "key does not exist"
  fix: "check .columns or .keys() first"
  never_try: ["catching KeyError silently"]
'''
        f = tmp_path / "patterns.yaml"
        f.write_text(yaml_content)
        ctx = failure_pattern_context(
            "KeyError: 'missing_col'",
            patterns_path=str(f),
        )
        assert ctx["metrics"]["has_match"] is True


# ---------------------------------------------------------------------------
# Simple YAML Parser Tests
# ---------------------------------------------------------------------------


class TestSimpleYamlParser:
    def test_basic_parsing(self):
        content = '''- pattern: "error one"
  root_cause: "cause one"
  fix: "fix one"
  never_try: ["a", "b"]

- pattern: "error two"
  root_cause: "cause two"
  fix: "fix two"
  never_try: ["c"]
'''
        result = _parse_simple_yaml(content)
        assert len(result) == 2
        assert result[0]["pattern"] == "error one"
        assert result[0]["never_try"] == ["a", "b"]
        assert result[1]["pattern"] == "error two"

    def test_multiline_never_try(self):
        content = '''- pattern: "some error"
  root_cause: "some cause"
  fix: "some fix"
  never_try:
    - "approach one"
    - "approach two"
'''
        result = _parse_simple_yaml(content)
        assert len(result) == 1
        assert result[0]["never_try"] == ["approach one", "approach two"]

    def test_empty_content(self):
        assert _parse_simple_yaml("") == []

    def test_comments_ignored(self):
        content = '''# This is a comment
- pattern: "test"
  root_cause: "cause"
  fix: "fix"
  # another comment
  never_try: []
'''
        result = _parse_simple_yaml(content)
        assert len(result) == 1


# ---------------------------------------------------------------------------
# Render Tests
# ---------------------------------------------------------------------------


class TestRender:
    def test_render_with_matches(self):
        ctx = failure_pattern_context(
            "fixture 'root' not found",
            patterns=SAMPLE_PATTERNS,
        )
        md = render_failure_pattern_report(ctx)
        assert "## Matched Patterns" in md
        assert "Root cause:" in md or "root cause:" in md.lower()
        assert "Fix:" in md or "fix:" in md.lower()
        assert "Never try:" in md or "never_try" in md.lower() or "\u26d4" in md

    def test_render_without_matches(self):
        ctx = failure_pattern_context(
            "unknown error xyz",
            patterns=SAMPLE_PATTERNS,
        )
        md = render_failure_pattern_report(ctx)
        assert "No known patterns matched" in md
