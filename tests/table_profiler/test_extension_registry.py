"""Tests for lib/extension_registry.py — F22 Domain-Specific Extensions."""

from __future__ import annotations

import sys

import pytest

sys.dont_write_bytecode = True
sys.path.insert(
    0, "/Workspace/Users/user@example.com/tools/table_profiler"
)

from tools.table_profiler_tool.lib.extension_registry import (
    register_semantic_pattern,
    get_registered_patterns,
    clear_registered_patterns,
    match_registered_patterns,
    match_column_values,
    PatternSpec,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clean_registry():
    """Ensure clean registry for each test."""
    clear_registered_patterns()
    yield
    clear_registered_patterns()


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class TestRegistration:
    """Tests for register_semantic_pattern."""

    def test_register_basic_pattern(self):
        spec = register_semantic_pattern(
            name="interconnection_number",
            regex=r"IC-\d{6}",
            description="Interconnection ID format",
            examples=["IC-001234", "IC-999999"],
        )
        assert spec.name == "interconnection_number"
        assert spec.regex == r"IC-\d{6}"
        assert spec.description == "Interconnection ID format"
        assert spec.examples == ["IC-001234", "IC-999999"]

    def test_register_appears_in_registry(self):
        register_semantic_pattern("test_pat", r"TP-\d+")
        patterns = get_registered_patterns()
        assert "test_pat" in patterns
        assert patterns["test_pat"].regex == r"TP-\d+"

    def test_register_overwrites_existing(self):
        register_semantic_pattern("dup", r"V1")
        register_semantic_pattern("dup", r"V2")
        assert get_registered_patterns()["dup"].regex == r"V2"

    def test_register_empty_name_raises(self):
        with pytest.raises(ValueError, match="name must not be empty"):
            register_semantic_pattern("", r"\d+")

    def test_register_empty_regex_raises(self):
        with pytest.raises(ValueError, match="regex must not be empty"):
            register_semantic_pattern("test", "")

    def test_register_invalid_regex_raises(self):
        with pytest.raises(ValueError, match="Invalid regex"):
            register_semantic_pattern("bad", r"[invalid")

    def test_clear_removes_all(self):
        register_semantic_pattern("a", r"A")
        register_semantic_pattern("b", r"B")
        clear_registered_patterns()
        assert get_registered_patterns() == {}


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


class TestMatching:
    """Tests for match_registered_patterns and match_column_values."""

    def test_match_single_pattern(self):
        register_semantic_pattern("project_code", r"PRJ-\d{4}")
        matches = match_registered_patterns("PRJ-1234")
        assert matches == ["project_code"]

    def test_no_match(self):
        register_semantic_pattern("project_code", r"PRJ-\d{4}")
        matches = match_registered_patterns("ABC-1234")
        assert matches == []

    def test_match_multiple_patterns(self):
        register_semantic_pattern("short_code", r"[A-Z]{3}")
        register_semantic_pattern("upper_alpha", r"[A-Z]+")
        matches = match_registered_patterns("ABC")
        assert "short_code" in matches
        assert "upper_alpha" in matches

    def test_match_empty_value(self):
        register_semantic_pattern("any", r".*")
        matches = match_registered_patterns("")
        assert matches == []

    def test_match_none_value(self):
        register_semantic_pattern("any", r".*")
        matches = match_registered_patterns(None)
        assert matches == []

    def test_column_values_above_threshold(self):
        register_semantic_pattern("zip5", r"\d{5}")
        values = ["12345", "67890", "11111", "22222", "33333"]
        result = match_column_values(values, threshold=0.80)
        assert result == "zip5"

    def test_column_values_below_threshold(self):
        register_semantic_pattern("zip5", r"\d{5}")
        values = ["12345", "abc", "def", "ghi", "jkl"]
        result = match_column_values(values, threshold=0.80)
        assert result is None

    def test_column_values_empty_list(self):
        register_semantic_pattern("zip5", r"\d{5}")
        result = match_column_values([])
        assert result is None

    def test_column_values_no_registry(self):
        result = match_column_values(["12345", "67890"])
        assert result is None
