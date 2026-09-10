"""Tests for review_context — pre-gate changeset review.

Tests verify diffstat computation, untested change detection,
acceptance criteria matching, and empty session handling.
"""
import pytest

from odibi_anchor.codebase.review_context import review_context


# ---------------------------------------------------------------------------
# Fixtures — synthetic session data
# ---------------------------------------------------------------------------

def _make_session_diff(per_file: dict, total_add: int = 0, total_del: int = 0) -> dict:
    """Build a synthetic session_diff dict."""
    if not total_add and not total_del:
        total_add = sum(f.get("additions", 0) for f in per_file.values())
        total_del = sum(f.get("deletions", 0) for f in per_file.values())
    return {
        "kind": "session_diff",
        "subject": "session",
        "summary": f"{len(per_file)} file(s): +{total_add}/-{total_del} lines",
        "metrics": {
            "files_changed": len(per_file),
            "total_additions": total_add,
            "total_deletions": total_del,
            "net_lines": total_add - total_del,
        },
        "findings": [],
        "risks": [],
        "samples": {"per_file": per_file},
        "suggested_next_actions": [],
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestBasicDiffstat:
    """Test diffstat computation from session_diff."""

    def test_basic_diffstat(self):
        """Computes correct adds/dels from session_diff."""
        diff = _make_session_diff({
            "src/module.py": {"status": "modified", "additions": 10, "deletions": 3, "diff": "+new code"},
            "src/utils.py": {"status": "new", "additions": 25, "deletions": 0, "diff": "+new file"},
        })
        ctx = review_context("/fake/root", session_diff=diff, output_format="dict")

        assert ctx["kind"] == "review_context"
        assert ctx["metrics"]["files_changed"] == 2
        assert ctx["metrics"]["additions"] == 35
        assert ctx["metrics"]["deletions"] == 3
        assert ctx["metrics"]["net_lines"] == 32

    def test_single_file_change(self):
        """Single file produces correct summary."""
        diff = _make_session_diff({
            "config.toml": {"status": "modified", "additions": 2, "deletions": 1, "diff": "+timeout=30"},
        })
        ctx = review_context("/fake/root", session_diff=diff, output_format="dict")
        assert "1 file(s) changed" in ctx["summary"]


class TestUntestedChanges:
    """Test untested .py change detection."""

    def test_untested_changes_flagged(self):
        """Changed .py with no test run → finding."""
        diff = _make_session_diff({
            "src/module.py": {"status": "modified", "additions": 10, "deletions": 0, "diff": "+code"},
        })
        ctx = review_context(
            "/fake/root",
            session_diff=diff,
            test_timings=[],  # No tests run
            files_changed={"src/module.py"},
            output_format="dict",
        )
        assert any("UNTESTED" in f for f in ctx["findings"])
        assert ctx["metrics"]["tests_run"] is False

    def test_tested_changes_clean(self):
        """Changed .py with tests run → no UNTESTED finding."""
        diff = _make_session_diff({
            "src/module.py": {"status": "modified", "additions": 5, "deletions": 2, "diff": "+code"},
        })
        ctx = review_context(
            "/fake/root",
            session_diff=diff,
            test_timings=[{"action": "test", "error": None, "passed": True, "test_target": "tests/test_module.py"}],
            files_changed={"src/module.py"},
            output_format="dict",
        )
        assert not any("UNTESTED" in f for f in ctx["findings"])

    def test_list_valued_test_target_is_supported(self):
        """Changed .py with list-targeted tests → no UNTESTED finding."""
        diff = _make_session_diff({
            "src/module.py": {"status": "modified", "additions": 5, "deletions": 2, "diff": "+code"},
        })
        test_timings = [{
            "action": "test",
            "error": None,
            "passed": True,
            "test_target": ["tests/test_module.py", "tests/test_utils.py"],
        }]

        ctx = review_context(
            "/fake/root",
            session_diff=diff,
            test_timings=test_timings,
            files_changed={"src/module.py"},
            output_format="dict",
        )

        assert ctx["kind"] == "review_context"
        assert ctx["metrics"]["tests_run"] is True
        assert not any("UNTESTED" in finding for finding in ctx["findings"])


class TestAcceptanceCriteria:
    """Test acceptance criteria matching against diff."""

    def test_acceptance_criteria_check(self):
        """Criteria matched against diff keywords."""
        diff = _make_session_diff({
            "src/enforcement.py": {
                "status": "modified",
                "additions": 20,
                "deletions": 5,
                "diff": "+def should_block_edit_limit(timings, max_ungated=6):",
            },
        })
        ctx = review_context(
            "/fake/root",
            session_diff=diff,
            acceptance_criteria=[
                "should_block_edit_limit function exists",
                "database migration applied",  # Not in diff
            ],
            output_format="dict",
        )
        criteria = ctx["samples"]["criteria"]
        assert criteria[0]["matched"] is True
        assert criteria[1]["matched"] is False
        assert ctx["metrics"]["criteria_matched"] == 1
        assert ctx["metrics"]["criteria_total"] == 2


class TestEmptySession:
    """Test empty session handling."""

    def test_empty_session(self):
        """No changes → 'nothing to review' summary."""
        ctx = review_context("/fake/root", session_diff=None, output_format="dict")
        assert "Nothing to review" in ctx["summary"]
        assert ctx["metrics"]["files_changed"] == 0

    def test_empty_diff_metrics(self):
        """session_diff with 0 files_changed → nothing to review."""
        empty_diff = {
            "metrics": {"files_changed": 0, "total_additions": 0, "total_deletions": 0},
            "samples": {"per_file": {}},
        }
        ctx = review_context("/fake/root", session_diff=empty_diff, output_format="dict")
        assert "Nothing to review" in ctx["summary"]
