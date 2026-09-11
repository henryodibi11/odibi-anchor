"""Tests for odibi_anchor._dispatcher._compliance — compliance scoring, status, audit.

Tests cover:
- _compliance_audit: 15-point scoring from session timings
- _build_status_suggestions: context-aware next-action suggestions
- _status: session health dashboard (dict and markdown)
- _audit_history: compliance trend display
- _skills_registry: skill listing from filesystem
"""

import os
import shutil
import tempfile
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest

_COMP = "odibi_anchor._dispatcher._compliance"


# ── Helpers ──────────────────────────────────────────────────────────────


def _t(action, error=None, passed=True, elapsed_ms=50.0, **extra):
    """Build a minimal timing record."""
    entry = {"action": action, "error": error, "passed": passed, "elapsed_ms": elapsed_ms}
    entry.update(extra)
    return entry


# ═════════════════════════════════════════════════════════════════════════
# _compliance_audit
# ═════════════════════════════════════════════════════════════════════════


class TestComplianceAudit:
    """Test the 15-point compliance scoring function."""

    def _run(self, timings, files_changed=None, files_created=None):
        """Patch globals and run _compliance_audit."""
        from odibi_anchor._dispatcher._compliance import _compliance_audit

        with patch(f"{_COMP}._SESSION_TIMINGS", timings), \
             patch(f"{_COMP}._SESSION_FILES_CHANGED", files_changed or set()), \
             patch(f"{_COMP}._SESSION_FILES_CREATED", files_created or set()):
            return _compliance_audit()

    def test_empty_session__minimal_gaps(self):
        result = self._run([], set(), set())
        assert result["max_score"] == 15
        assert result["score"] <= 15
        assert result["stats"]["total_actions"] == 0
        assert result["stats"]["files_changed"] == 0
        assert isinstance(result["rating"], str)

    def test_perfect_session__high_score(self):
        timings = [
            _t("status"),
            _t("memory"),
            _t("audit_history"),
            _t("new_session"),
            _t("task"),
            _t("known_bad"),
            _t("touched"),
            _t("touched"),
            _t("preflight"),
            _t("test"),
            _t("gate", passed=True),
        ]
        files = {"src/foo.py", "src/bar.py"}
        result = self._run(timings, files)
        # Perfect session should have very few (ideally 0) gaps
        assert result["score"] >= 12
        assert result["rating"] in ("excellent", "good")

    def test_no_planning_before_edits__gap(self):
        timings = [
            _t("touched"),
            _t("safe"),
        ]
        files = {"src/foo.py"}
        result = self._run(timings, files)
        gap_texts = " ".join(result["gaps"])
        assert "planning" in gap_texts.lower() or "No planning" in gap_texts

    def test_planning_after_edit__gap(self):
        timings = [
            _t("touched"),
            _t("task"),
        ]
        files = {"src/foo.py"}
        result = self._run(timings, files)
        gap_texts = " ".join(result["gaps"])
        assert "AFTER" in gap_texts or "after" in gap_texts.lower()

    def test_no_known_bad_for_py__gap(self):
        timings = [
            _t("task"),
            _t("touched"),
        ]
        files = {"src/foo.py"}
        result = self._run(timings, files)
        gap_texts = " ".join(result["gaps"])
        assert "known_bad" in gap_texts

    def test_no_tests_when_files_changed__gap(self):
        timings = [
            _t("task"),
            _t("touched"),
        ]
        files = {"src/foo.py"}
        result = self._run(timings, files)
        gap_texts = " ".join(result["gaps"])
        assert "test" in gap_texts.lower()

    def test_no_gate_when_files_changed__gap(self):
        timings = [
            _t("task"),
            _t("known_bad"),
            _t("touched"),
            _t("test"),
        ]
        files = {"src/foo.py"}
        result = self._run(timings, files)
        gap_texts = " ".join(result["gaps"])
        assert "gate" in gap_texts.lower() or "checkpoint" in gap_texts.lower()

    def test_high_error_ratio__gap(self):
        timings = [
            _t("status"),
            _t("task", error="RuntimeError"),
            _t("task", error="RuntimeError"),
            _t("task", error="RuntimeError"),
            _t("task"),
            _t("touched"),
        ]
        files = {"src/foo.py"}
        result = self._run(timings, files)
        gap_texts = " ".join(result["gaps"])
        assert "error ratio" in gap_texts.lower() or "thrashing" in gap_texts.lower()

    def test_retry_loop__gap(self):
        timings = [
            _t("status"),
            _t("gate"),
            _t("gate"),
            _t("gate"),
        ]
        result = self._run(timings)
        gap_texts = " ".join(result["gaps"])
        assert "retry" in gap_texts.lower() or "consecutive" in gap_texts.lower()

    def test_low_tool_diversity__gap(self):
        timings = [
            _t("touched"),
            _t("touched"),
            _t("touched"),
        ]
        files = {"a.py", "b.py", "c.py"}
        result = self._run(timings, files)
        gap_texts = " ".join(result["gaps"])
        assert "diversity" in gap_texts.lower()

    def test_cowboy_coding__gap(self):
        timings = [
            _t("touched"),
        ]
        files = {"foo.py"}
        result = self._run(timings, files)
        gap_texts = " ".join(result["gaps"])
        assert "cowboy" in gap_texts.lower() or "read/plan" in gap_texts.lower()

    def test_no_preflight_for_py__gap(self):
        timings = [
            _t("task"),
            _t("known_bad"),
            _t("touched"),
            _t("test"),
            _t("gate"),
        ]
        files = {"src/foo.py"}
        result = self._run(timings, files)
        gap_texts = " ".join(result["gaps"])
        assert "preflight" in gap_texts.lower()

    def test_changed_registry_does_not_require_raw_touched_count(self):
        timings = [
            _t("task"),
            _t("touched"),
        ]
        files = {"a.py", "b.py", "c.py"}
        result = self._run(timings, files)
        gap_texts = " ".join(result["gaps"])
        assert "touched called" not in gap_texts.lower()

    def test_non_python_changes_need_neither_tests_nor_preflight(self):
        result = self._run([_t("task"), _t("gate")], {"README.md"})
        gaps = " ".join(result["gaps"]).lower()
        assert "test" not in gaps
        assert "preflight" not in gaps

    def test_expected_registration_repetition_is_not_retry(self):
        result = self._run([_t("skill_loaded")] * 3 + [_t("touched")] * 3)
        assert "retry" not in " ".join(result["gaps"]).lower()

    def test_repeatable_actions_do_not_collapse_other_actions_into_retry(self):
        timings = [_t("gate"), _t("skill_loaded"), _t("gate"), _t("touched"), _t("gate")]
        result = self._run(timings)
        assert "retry" not in " ".join(result["gaps"]).lower()

    def test_non_passing_task_does_not_count_as_planning(self):
        timings = [_t("task", passed=False), _t("touched")]
        gaps = " ".join(self._run(timings, {"README.md"})["gaps"]).lower()
        assert "no planning" in gaps

    def test_failed_success_sensitive_actions_do_not_count(self):
        timings = [_t("task"), _t("known_bad"), _t("preflight", passed=False),
                   _t("test", passed=False), _t("gate", passed=False)]
        gaps = " ".join(self._run(timings, {"src/foo.py"})["gaps"]).lower()
        assert "preflight" in gaps and "tests" in gaps and "gate" in gaps

    def test_previous_checkpoint_cannot_satisfy_current_delivery(self):
        timings = [
            _t("task"), _t("known_bad"), _t("touched"), _t("preflight"),
            _t("test"), _t("gate"),
            _t("checkpoint", includes_learn=True),
            _t("task"), _t("touched"),
        ]
        gaps = " ".join(self._run(timings, {"src/foo.py"})["gaps"]).lower()
        assert "preflight" in gaps and "tests" in gaps and "gate" in gaps

    def test_previous_manual_gate_cannot_mask_current_failures(self):
        timings = [
            _t("task"), _t("known_bad"), _t("touched"),
            _t("preflight"), _t("test"), _t("gate"),
            _t("task"), _t("touched"),
            _t("preflight", passed=False), _t("test", passed=False),
            _t("gate", passed=False),
        ]
        gaps = " ".join(self._run(timings, {"src/foo.py"})["gaps"]).lower()
        assert "preflight" in gaps and "tests" in gaps and "gate" in gaps

    def test_four_files_do_not_imply_multiple_features(self):
        timings = [_t("task"), _t("gate")]
        gaps = " ".join(self._run(timings, {f"docs/{i}.md" for i in range(4)})["gaps"])
        assert "4+" not in gaps and "between features" not in gaps

    def test_rating_bands(self):
        # Excellent: score >= 13
        result = self._run([])
        # Empty session has 0 files so most checks pass
        assert result["rating"] in ("excellent", "good", "needs_improvement", "poor")

    def test_stats_populated(self):
        timings = [_t("status"), _t("task", error="RuntimeError"), _t("memory")]
        result = self._run(timings)
        assert result["stats"]["total_actions"] == 3
        assert result["stats"]["errors_encountered"] == 1
        # unique actions with error=None: status, memory → 2
        assert result["stats"]["unique_actions"] == 2

    def test_checkpoint_satisfies_gate_and_test(self):
        timings = [
            _t("task"),
            _t("known_bad"),
            _t("touched"),
            _t("preflight"),
            _t("checkpoint", passed=True),
        ]
        files = {"src/foo.py"}
        result = self._run(timings, files)
        # checkpoint should count for both gate and test
        gap_texts = " ".join(result["gaps"])
        assert "No gate" not in gap_texts
        assert "No tests" not in gap_texts


# ═════════════════════════════════════════════════════════════════════════
# _build_status_suggestions
# ═════════════════════════════════════════════════════════════════════════


class TestBuildStatusSuggestions:
    """Test context-aware suggestion builder."""

    def _run(self, obligations, timings=None, files_changed=None):
        from odibi_anchor._dispatcher._compliance import _build_status_suggestions

        with patch(f"{_COMP}._SESSION_TIMINGS", timings or []), \
             patch(f"{_COMP}._SESSION_FILES_CHANGED", files_changed or set()):
            return _build_status_suggestions(obligations)

    def test_with_obligations__must_first(self):
        result = self._run(["preflight (MUST)"])
        assert any("MUST" in s for s in result)

    def test_no_obligations__ready(self):
        result = self._run([])
        assert any("ready" in s.lower() or "clean" in s.lower() for s in result)

    def test_files_changed_no_task__must_planning(self):
        result = self._run([], timings=[], files_changed={"foo.py"})
        assert any("planning" in s.lower() or "task" in s.lower() for s in result)

    def test_long_session__thread_discipline(self):
        timings = [_t(f"action_{i}") for i in range(16)]
        result = self._run([], timings=timings)
        assert any("thread" in s.lower() or "ALERT" in s for s in result)

    def test_py_files_no_test__should_test(self):
        timings = [_t("task")]
        result = self._run([], timings=timings,
                           files_changed={"src/foo.py"})
        assert any("test" in s.lower() for s in result)


# ═════════════════════════════════════════════════════════════════════════
# _status
# ═════════════════════════════════════════════════════════════════════════


class TestStatus:
    """Test session health dashboard."""

    def _run(self, timings=None, files_changed=None, files_created=None,
             manifest=None, session_frame=None, output_format="dict"):
        from odibi_anchor._dispatcher._compliance import _status

        with patch(f"{_COMP}._SESSION_TIMINGS", timings or []), \
             patch(f"{_COMP}._SESSION_FILES_CHANGED", files_changed or set()), \
             patch(f"{_COMP}._SESSION_FILES_CREATED", files_created or set()), \
             patch(f"{_COMP}._db_audit_trend", return_value={"total_audits": 0, "avg_score": 0, "max_score": 15, "trend": "stable", "recent_scores": [], "top_gaps": []}):
            return _status(
                "/fake/root", manifest or {}, session_frame, {},
                output_format=output_format,
            )

    def test_empty_session__low_risk(self):
        result = self._run()
        assert isinstance(result, dict)
        assert result["metrics"]["estimated_risk"] == "low"
        assert result["metrics"]["files_changed"] == 0
        assert result["metrics"]["obligations_pending"] == 0

    def test_files_changed_no_gate__obligations(self):
        timings = [_t("task"), _t("touched")]
        result = self._run(timings=timings, files_changed={"src/foo.py"})
        assert result["metrics"]["obligations_pending"] > 0
        assert result["metrics"]["estimated_risk"] in ("medium", "high")

    def test_markdown_output__has_sections(self):
        result = self._run(output_format="markdown")
        assert isinstance(result, str)
        assert "# Session Status" in result
        assert "## Metrics" in result

    def test_next_required_action__follows_protocol(self):
        result = self._run()
        # Empty session → first step in STARTUP_SEQUENCE
        assert result["metrics"]["next_required_action"] is not None
        assert "status" in result["metrics"]["next_required_action"]

    def test_files_created_tracked(self):
        result = self._run(files_created={"new.py"})
        assert result["metrics"]["files_created"] == 1

    def test_kind_is_session_status(self):
        result = self._run()
        assert result["kind"] == "session_status"

    def test_contract_keys_present(self):
        result = self._run()
        for key in ("kind", "subject", "summary", "metrics", "findings",
                     "risks", "samples", "suggested_next_actions"):
            assert key in result


# ═════════════════════════════════════════════════════════════════════════
# _audit_history
# ═════════════════════════════════════════════════════════════════════════


class TestAuditHistory:
    """Test compliance audit history display."""

    @pytest.fixture
    def mock_trend(self):
        return {
            "total_audits": 5,
            "avg_score": 12.0,
            "max_score": 15,
            "trend": "improving",
            "recent_scores": [10, 11, 12, 13, 14],
            "top_gaps": [
                {"gap": "No preflight run", "count": 3},
                {"gap": "No known_bad check", "count": 2},
            ],
        }

    @pytest.fixture
    def mock_audits(self):
        return [
            {
                "created": "2025-01-15T10:00:00",
                "score": 13,
                "max_score": 15,
                "rating": "excellent",
                "gaps": ["No preflight"],
                "files_changed": ["foo.py"],
            },
            {
                "created": "2025-01-14T10:00:00",
                "score": 10,
                "max_score": 15,
                "rating": "good",
                "gaps": ["No tests", "No preflight"],
                "files_changed": ["bar.py", "baz.py"],
            },
        ]

    def test_dict_output(self, mock_trend, mock_audits):
        from odibi_anchor._dispatcher._compliance import _audit_history

        with patch(f"{_COMP}._db_audit_trend", return_value=mock_trend), \
             patch(f"{_COMP}._db_query_audits", return_value=mock_audits):
            result = _audit_history("/fake/root", output_format="dict")

        assert isinstance(result, dict)
        assert result["kind"] == "audit_history"
        assert result["metrics"]["total_audits"] == 5
        assert result["metrics"]["trend"] == "improving"
        assert len(result["findings"]) == 2

    def test_markdown_output(self, mock_trend, mock_audits):
        from odibi_anchor._dispatcher._compliance import _audit_history

        with patch(f"{_COMP}._db_audit_trend", return_value=mock_trend), \
             patch(f"{_COMP}._db_query_audits", return_value=mock_audits):
            result = _audit_history("/fake/root", output_format="markdown")

        assert isinstance(result, str)
        assert "# Compliance Audit History" in result
        assert "improving" in result
        assert "No preflight" in result

    def test_empty_history(self):
        from odibi_anchor._dispatcher._compliance import _audit_history
        empty_trend = {
            "total_audits": 0, "avg_score": 0, "max_score": 15,
            "trend": "stable", "recent_scores": [], "top_gaps": [],
        }
        with patch(f"{_COMP}._db_audit_trend", return_value=empty_trend), \
             patch(f"{_COMP}._db_query_audits", return_value=[]):
            result = _audit_history("/fake/root", output_format="dict")

        assert result["metrics"]["total_audits"] == 0


# ═════════════════════════════════════════════════════════════════════════
# _skills_registry
# ═════════════════════════════════════════════════════════════════════════


class TestSkillsRegistry:
    """Test skill listing from the central Odibi Anchor directory."""

    @staticmethod
    def _guidance_copy(tmp_path):
        source = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                              ".assistant", "skills")
        destination = tmp_path / "central-skills"
        shutil.copytree(source, destination)
        return destination

    def test_with_skills(self, tmp_path):
        from odibi_anchor._dispatcher._compliance import _skills_registry
        from odibi_anchor._dispatcher._boot import _ENV

        skills = self._guidance_copy(tmp_path)
        state = SimpleNamespace(skills_loaded={"code-comprehension", "writing-specs"})

        with patch(f"{_COMP}._SESSION_TIMINGS", []), \
             patch.dict(_ENV, {"skills_dir": str(skills)}):
            result = _skills_registry(str(tmp_path / "project"), session_state=state,
                                      output_format="dict")

        assert isinstance(result, dict)
        assert result["kind"] == "skills_registry"
        assert result["metrics"]["total_skills"] == 18
        assert result["metrics"]["loaded_count"] == 2
        assert result["metrics"]["loaded_skills"] == ["code-comprehension", "writing-specs"]
        assert len(result["skills"]) == 18

    def test_empty_directory(self, tmp_path):
        from odibi_anchor._dispatcher._compliance import _skills_registry
        from odibi_anchor._dispatcher._boot import _ENV

        empty_skills = tmp_path / "empty-skills"
        empty_skills.mkdir()
        with patch(f"{_COMP}._SESSION_TIMINGS", []), \
             patch.dict(_ENV, {"skills_dir": str(empty_skills)}):
            result = _skills_registry(str(tmp_path), output_format="dict")

        assert isinstance(result, dict)
        assert result["metrics"]["total_skills"] == 0

    def test_malformed_nonempty_directory_fails(self, tmp_path):
        from odibi_anchor._dispatcher._compliance import _skills_registry
        from odibi_anchor._dispatcher._boot import _ENV

        skills = tmp_path / "partial-skills"
        skills.mkdir()
        (skills / "README.md").write_text("not a registry", encoding="utf-8")
        with patch.dict(_ENV, {"skills_dir": str(skills)}), \
             pytest.raises(ValueError, match="Native skill 'auditing-memory-governance' does not exist"):
            _skills_registry(str(tmp_path), session_state=SimpleNamespace(skills_loaded=set()),
                             output_format="dict")

    def test_markdown_output(self, tmp_path):
        from odibi_anchor._dispatcher._compliance import _skills_registry
        from odibi_anchor._dispatcher._boot import _ENV

        skills = self._guidance_copy(tmp_path)
        with patch(f"{_COMP}._SESSION_TIMINGS", []), \
             patch.dict(_ENV, {"skills_dir": str(skills)}):
            result = _skills_registry(str(tmp_path),
                                      session_state=SimpleNamespace(skills_loaded={"writing-specs"}),
                                      output_format="markdown")

        assert isinstance(result, str)
        assert "# Available Skills" in result
        assert "18 skills available, 1 loaded" in result
        assert "| writing-specs |" in result and "loaded" in result
