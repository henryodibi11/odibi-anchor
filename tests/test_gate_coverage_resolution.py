"""Tests for gate test-coverage resolution (multi-strategy).

Verifies that check_test_coverage correctly resolves test coverage
using test_map, stem matching, and unmapped passthrough.
"""

import pytest

from odibi_anchor._dispatcher._enforcement import check_test_coverage


class TestCheckTestCoverage:
    """Tests for the multi-strategy test coverage resolution."""

    def test_frame_map_match(self):
        """test_target in test_map for changed file → covered."""
        test_map = {
            "src/odibi_anchor/codebase/codebase_map_context.py": [
                "tests/test_codebase_map_context.py",
                "tests/test_output_schema.py",
            ],
        }

        is_covered, uncovered = check_test_coverage(
            changed_py=["src/odibi_anchor/codebase/codebase_map_context.py"],
            test_target="tests/test_codebase_map_context.py",
            test_map=test_map,
        )
        assert is_covered is True
        assert uncovered == []

    def test_frame_map_mismatch(self):
        """test_target NOT in test_map entry → uncovered (blocks)."""
        test_map = {
            "src/odibi_anchor/codebase/codebase_map_context.py": [
                "tests/test_codebase_map_context.py",
            ],
        }

        is_covered, uncovered = check_test_coverage(
            changed_py=["src/odibi_anchor/codebase/codebase_map_context.py"],
            test_target="tests/test_something_else.py",
            test_map=test_map,
        )
        assert is_covered is False
        assert "src/odibi_anchor/codebase/codebase_map_context.py" in uncovered

    def test_stem_fallback(self):
        """No test_map, but test_foo.py covers foo.py by stem."""
        is_covered, uncovered = check_test_coverage(
            changed_py=["src/odibi_anchor/codebase/safe_change_context.py"],
            test_target="tests/codebase/test_safe_change_context.py",
            test_map=None,
        )
        assert is_covered is True
        assert uncovered == []

    def test_unmapped_passthrough(self):
        """File not in test_map, no stem match → pass (not block)."""
        test_map = {
            # agent_init.py is NOT in the map
            "src/odibi_anchor/codebase/codebase_map_context.py": [
                "tests/test_codebase_map_context.py",
            ],
        }

        is_covered, uncovered = check_test_coverage(
            changed_py=["agent_init.py"],
            test_target="tests/test_codebase_map_context.py",
            test_map=test_map,
        )
        # agent_init.py is unmapped → should NOT block
        assert is_covered is True
        assert uncovered == []

    def test_multiple_changed_files_mixed(self):
        """Mix of covered (via map) and unmapped → passes."""
        test_map = {
            "src/odibi_anchor/codebase/codebase_map_context.py": [
                "tests/test_codebase_map_context.py",
            ],
        }

        is_covered, uncovered = check_test_coverage(
            changed_py=[
                "src/odibi_anchor/codebase/codebase_map_context.py",  # Covered by map
                "agent_init.py",  # Unmapped → passthrough
            ],
            test_target="tests/test_codebase_map_context.py",
            test_map=test_map,
        )
        assert is_covered is True
        assert uncovered == []

    def test_all_uncovered_blocks(self):
        """Target covers none of the changed files → blocks."""
        test_map = {
            "src/odibi_anchor/codebase/codebase_map_context.py": [
                "tests/test_codebase_map_context.py",
            ],
            "src/odibi_anchor/codebase/safe_change_context.py": [
                "tests/codebase/test_safe_change_context.py",
            ],
        }

        # Target doesn't match either mapped entry
        is_covered, uncovered = check_test_coverage(
            changed_py=[
                "src/odibi_anchor/codebase/codebase_map_context.py",
                "src/odibi_anchor/codebase/safe_change_context.py",
            ],
            test_target="tests/test_completely_unrelated.py",
            test_map=test_map,
        )
        assert is_covered is False
        assert len(uncovered) == 2

    def test_auto_scoped_target_collection_covers_all_mapped_files(self):
        test_map = {
            "src/alpha.py": ["tests/test_alpha.py"],
            "src/beta.py": ["tests/test_beta.py"],
        }

        is_covered, uncovered = check_test_coverage(
            changed_py=["src/alpha.py", "src/beta.py"],
            test_target=["tests/test_alpha.py", "tests/test_beta.py"],
            test_map=test_map,
        )

        assert is_covered is True
        assert uncovered == []

    def test_explicit_tests_directory_is_full_suite_evidence(self):
        test_map = {"src/alpha.py": ["tests/unit/test_alpha.py"]}

        is_covered, uncovered = check_test_coverage(
            changed_py=["src/alpha.py"],
            test_target="tests/",
            test_map=test_map,
        )

        assert is_covered is True
        assert uncovered == []

    def test_windows_map_key_does_not_degrade_to_unmapped_passthrough(self):
        test_map = {r"src\pkg\alpha.py": [r"tests\test_alpha.py"]}

        is_covered, uncovered = check_test_coverage(
            changed_py=["src/pkg/alpha.py"],
            test_target="tests/test_unrelated.py",
            test_map=test_map,
        )

        assert is_covered is False
        assert uncovered == ["src/pkg/alpha.py"]
