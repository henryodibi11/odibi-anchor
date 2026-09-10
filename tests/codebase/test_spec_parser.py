"""Tests for _spec_parser.py — Phase 1 spec frontmatter parser."""

from odibi_anchor.codebase._spec_parser import (
    _normalize_status,
    _parse_bold_text,
    _parse_frontmatter,
    find_spec,
    list_specs,
    parse_spec,
)

# ---------------------------------------------------------------------------
# Status normalization
# ---------------------------------------------------------------------------


class TestNormalizeStatus:
    """Tests for _normalize_status helper."""

    def test_simple_ready(self):
        assert _normalize_status("ready") == "ready"

    def test_executing_is_preserved(self):
        assert _normalize_status("executing") == "executing"

    def test_simple_done(self):
        assert _normalize_status("done") == "done"

    def test_simple_draft(self):
        assert _normalize_status("draft") == "draft"

    def test_in_progress(self):
        assert _normalize_status("in-progress") == "in-progress"

    def test_emoji_prefix_done(self):
        """Status with emoji prefix and annotation."""
        assert _normalize_status("\u2705 Done \u2014 all 6 rules implemented (699 lines)") == "done"

    def test_emoji_prefix_ready(self):
        assert _normalize_status("\U0001f4cb ready") == "ready"

    def test_trailing_whitespace(self):
        assert _normalize_status("ready  ") == "ready"

    def test_unknown_defaults_to_draft(self):
        assert _normalize_status("something weird") == "draft"

    def test_abandoned(self):
        assert _normalize_status("abandoned") == "abandoned"


# ---------------------------------------------------------------------------
# YAML frontmatter parsing
# ---------------------------------------------------------------------------


class TestParseFrontmatter:
    """Tests for YAML frontmatter extraction."""

    def test_full_frontmatter(self):
        text = """---
status: in-progress
complexity: medium
estimated_sessions: 3
phases:
  - name: "Phase 1", status: done
  - name: "Phase 2", status: in-progress
success_criteria:
  - "Test criterion 1"
  - "Test criterion 2"
files_touched:
  - "src/module.py"
---

# Spec Title
"""
        result = _parse_frontmatter(text)
        assert result is not None
        assert result["status"] == "in-progress"
        assert result["complexity"] == "medium"
        assert result["estimated_sessions"] == "3"
        assert len(result["phases"]) == 2
        assert result["phases"][0]["name"] == "Phase 1"
        assert result["phases"][0]["status"] == "done"
        assert len(result["success_criteria"]) == 2
        assert result["success_criteria"][0] == "Test criterion 1"
        assert len(result["files_touched"]) == 1

    def test_empty_lists(self):
        text = """---
status: draft
complexity: null
phases: []
success_criteria: []
files_touched: []
---

# Empty Spec
"""
        result = _parse_frontmatter(text)
        assert result is not None
        assert result["status"] == "draft"
        assert result["complexity"] is None
        assert result["phases"] == []
        assert result["success_criteria"] == []

    def test_no_frontmatter_returns_none(self):
        text = """# Just a Title

**Status:** ready
"""
        result = _parse_frontmatter(text)
        assert result is None

    def test_comments_stripped_from_values(self):
        text = """---
status: ready          # draft | ready | in-progress | done
complexity: high
---

# Spec
"""
        result = _parse_frontmatter(text)
        assert result is not None
        assert result["status"] == "ready"


# ---------------------------------------------------------------------------
# Bold-text fallback parsing
# ---------------------------------------------------------------------------


class TestParseBoldText:
    """Tests for legacy bold-text format parsing."""

    def test_standard_format(self):
        text = """# My Spec

**Status:** ready  
**Complexity:** Medium  
**Estimated:** 2 sessions  
"""
        result = _parse_bold_text(text)
        assert result["status"] == "ready"
        assert result["complexity"] == "medium"
        assert result["estimated_sessions"] == 2

    def test_done_with_annotation(self):
        text = """# Done Spec

**Status:** \u2705 Done \u2014 all rules implemented  
**Complexity:** Medium \u2014 ~250 new lines  
**Estimated:** 1\u20132 sessions (completed in 1)
"""
        result = _parse_bold_text(text)
        assert result["status"] == "done"
        assert result["complexity"] == "medium"
        assert result["estimated_sessions"] == 1

    def test_success_criteria_extraction(self):
        text = """# Spec

**Status:** ready

## Success Criteria

- Criterion one works
- Criterion two passes
- Third criterion verified

## Next Section
"""
        result = _parse_bold_text(text)
        assert len(result["success_criteria"]) == 3
        assert result["success_criteria"][0] == "Criterion one works"

    def test_first_match_only(self):
        """Only the first **Status:** match should be used."""
        text = """# Spec

**Status:** ready

## Example

**Status:** draft
"""
        result = _parse_bold_text(text)
        assert result["status"] == "ready"


# ---------------------------------------------------------------------------
# parse_spec (integration)
# ---------------------------------------------------------------------------


class TestParseSpec:
    """Integration tests for parse_spec."""

    def test_yaml_frontmatter_spec(self, tmp_path):
        spec = tmp_path / "FEATURE_X_SPEC.md"
        spec.write_text("""---
status: ready
complexity: low
estimated_sessions: 1
phases: []
success_criteria:
  - "It works"
files_touched: []
---

# Feature X
""", encoding="utf-8")
        result = parse_spec(spec)
        assert result["name"] == "FEATURE_X"
        assert result["status"] == "ready"
        assert result["complexity"] == "low"
        assert result["estimated_sessions"] == 1
        assert result["success_criteria"] == ["It works"]
        assert result["raw_path"] == str(spec)

    def test_string_phase_entries_are_normalized(self, tmp_path):
        spec = tmp_path / "CONCISE_PHASES_SPEC.md"
        spec.write_text("""---
status: in-progress
phases:
  - "Stabilize runtime"
  - "Verify lifecycle"
---

# Concise phases
""", encoding="utf-8")

        result = parse_spec(spec)

        assert result["phases"] == [
            {"name": "Stabilize runtime", "status": "not-started"},
            {"name": "Verify lifecycle", "status": "not-started"},
        ]

    def test_done_spec_marks_concise_string_phases_done(self, tmp_path):
        spec = tmp_path / "DONE_CONCISE_PHASES_SPEC.md"
        spec.write_text("""---
status: done
phases:
  - "Stabilize runtime"
  - "Verify lifecycle"
---

# Done concise phases
""", encoding="utf-8")

        result = parse_spec(spec)

        assert result["phases"] == [
            {"name": "Stabilize runtime", "status": "done"},
            {"name": "Verify lifecycle", "status": "done"},
        ]

    def test_done_spec_preserves_explicit_phase_mapping(self, tmp_path):
        spec = tmp_path / "MIXED_PHASES_SPEC.md"
        spec.write_text("""---
status: ✅ Done — verified
phases:
  - "Inferred phase"
  - name: "Explicit exception"
    status: blocked
---

# Mixed phases
""", encoding="utf-8")

        result = parse_spec(spec)

        assert result["status"] == "done"
        assert result["phases"] == [
            {"name": "Inferred phase", "status": "done"},
            {"name": "Explicit exception", "status": "blocked"},
        ]

    def test_bold_text_spec(self, tmp_path):
        spec = tmp_path / "MY_FEATURE_SPEC.md"
        spec.write_text("""# My Feature Specification

**Status:** done  
**Complexity:** High \u2014 complex logic  
**Estimated:** 3 sessions  

## Success Criteria

- All tests pass
- No regressions
""", encoding="utf-8")
        result = parse_spec(spec)
        assert result["name"] == "MY_FEATURE"
        assert result["status"] == "done"
        assert result["complexity"] == "high"
        assert result["estimated_sessions"] == 3
        assert len(result["success_criteria"]) == 2

    def test_name_strips_spec_suffix(self, tmp_path):
        spec = tmp_path / "HELLO_WORLD_SPEC.md"
        spec.write_text("# Hello\n\n**Status:** draft\n", encoding="utf-8")
        result = parse_spec(spec)
        assert result["name"] == "HELLO_WORLD"

# ---------------------------------------------------------------------------
# list_specs
# ---------------------------------------------------------------------------


class TestListSpecs:
    """Tests for list_specs directory scanning."""

    def test_lists_all_spec_files(self, tmp_path):
        (tmp_path / "A_SPEC.md").write_text("# A\n\n**Status:** ready\n", encoding="utf-8")
        (tmp_path / "B_SPEC.md").write_text("# B\n\n**Status:** done\n", encoding="utf-8")
        (tmp_path / "C_SPEC.md").write_text("# C\n\n**Status:** draft\n", encoding="utf-8")
        (tmp_path / "not_a_spec.md").write_text("# Not a spec\n", encoding="utf-8")

        results = list_specs(tmp_path)
        assert len(results) == 3
        names = [r["name"] for r in results]
        assert "A" in names
        assert "B" in names
        assert "C" in names

    def test_empty_directory(self, tmp_path):
        results = list_specs(tmp_path)
        assert results == []

    def test_nonexistent_directory(self):
        results = list_specs("/nonexistent/path")
        assert results == []


# ---------------------------------------------------------------------------
# find_spec
# ---------------------------------------------------------------------------


class TestFindSpec:
    """Tests for find_spec name matching."""

    def test_exact_name(self, tmp_path):
        (tmp_path / "CONVENTION_AUTOMATION_SPEC.md").write_text(
            "# Conv\n\n**Status:** done\n", encoding="utf-8"
        )
        result = find_spec(tmp_path, "CONVENTION_AUTOMATION")
        assert result is not None
        assert result["name"] == "CONVENTION_AUTOMATION"

    def test_partial_name(self, tmp_path):
        (tmp_path / "CONVENTION_AUTOMATION_SPEC.md").write_text(
            "# Conv\n\n**Status:** done\n", encoding="utf-8"
        )
        result = find_spec(tmp_path, "convention")
        assert result is not None
        assert result["name"] == "CONVENTION_AUTOMATION"

    def test_case_insensitive(self, tmp_path):
        (tmp_path / "MY_FEATURE_SPEC.md").write_text(
            "# My\n\n**Status:** ready\n", encoding="utf-8"
        )
        result = find_spec(tmp_path, "my_feature")
        assert result is not None

    def test_no_match_returns_none(self, tmp_path):
        (tmp_path / "OTHER_SPEC.md").write_text("# Other\n\n**Status:** done\n", encoding="utf-8")
        result = find_spec(tmp_path, "nonexistent")
        assert result is None
