"""Tests for gate timing verification (v0.5.1).

Verifies:
    - Claimed obligations without timing proof are rejected
    - Claimed obligations with timing proof are verified
    - Exempt obligations (pytest) pass without timing
    - Unknown obligations pass (forward compatibility)
    - skip_timing_verification bypasses all checks
    - learn_reminder emitted on gate PASS with files_changed
    - Rejected obligations appear in risks and findings
"""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from odibi_anchor.codebase.workflow_gate_context import (
    _verify_timing_proof,
    workflow_gate_context,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def mock_timings():
    """Provide a controlled timings list for tests.
    
    Unit tests pass directly via _timings param.
    Integration tests pass via _timings_override kwarg to gate.
    """
    return []


@pytest.fixture
def project_root(tmp_path):
    """Minimal project for gate."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "mod.py").write_text("pass\n")
    (tmp_path / "tests").mkdir()
    return tmp_path


# ---------------------------------------------------------------------------
# _verify_timing_proof unit tests
# ---------------------------------------------------------------------------


class TestVerifyTimingProof:
    """Direct tests for the verification function."""

    @pytest.mark.fast
    def test_empty_obligations_returns_empty(self, mock_timings):
        verified, rejected = _verify_timing_proof(set(), _timings=mock_timings)
        assert verified == []
        assert rejected == []

    @pytest.mark.fast
    def test_obligation_with_matching_timing_verified(self, mock_timings):
        mock_timings.append({"action": "preflight", "elapsed_ms": 100, "error": None})
        verified, rejected = _verify_timing_proof({"preflight_context"}, _timings=mock_timings)
        assert "preflight_context" in verified
        assert rejected == []

    @pytest.mark.fast
    def test_obligation_without_timing_rejected(self, mock_timings):
        # No timings at all
        verified, rejected = _verify_timing_proof({"preflight_context"}, _timings=mock_timings)
        assert verified == []
        assert "preflight_context" in rejected

    @pytest.mark.fast
    def test_safe_satisfies_multiple_obligations(self, mock_timings):
        """anchor('safe') auto-chains preflight, test_focus, known_bad -- all should verify."""
        mock_timings.append({"action": "safe", "elapsed_ms": 500, "error": None})
        verified, rejected = _verify_timing_proof(
            {"preflight_context", "test_focus_context", "known_bad_change_context"},
            _timings=mock_timings,
        )
        assert set(verified) == {"preflight_context", "test_focus_context", "known_bad_change_context"}
        assert rejected == []

    @pytest.mark.fast
    def test_task_satisfies_planning(self, mock_timings):
        """'task' satisfies task_execution_context (full planning — no quick path)."""
        mock_timings.append({"action": "task", "elapsed_ms": 50, "error": None})
        verified, rejected = _verify_timing_proof({"task_execution_context"}, _timings=mock_timings)
        assert "task_execution_context" in verified

    @pytest.mark.fast
    def test_exempt_obligation_passes_without_timing(self, mock_timings):
        """pytest is exempt from timing verification."""
        verified, rejected = _verify_timing_proof({"pytest"}, _timings=mock_timings)
        assert "pytest" in verified
        assert rejected == []

    @pytest.mark.fast
    def test_unknown_obligation_passes(self, mock_timings):
        """Unknown obligations pass for forward compatibility."""
        verified, rejected = _verify_timing_proof({"future_tool_context"}, _timings=mock_timings)
        assert "future_tool_context" in verified
        assert rejected == []

    @pytest.mark.fast
    def test_errored_timing_does_not_count(self, mock_timings):
        """Actions that errored don't satisfy obligations."""
        mock_timings.append({"action": "preflight", "elapsed_ms": 50, "error": "ValueError"})
        verified, rejected = _verify_timing_proof({"preflight_context"}, _timings=mock_timings)
        assert "preflight_context" in rejected

    @pytest.mark.fast
    def test_skip_verification_bypasses_all(self, mock_timings):
        """skip_verification=True accepts everything."""
        verified, rejected = _verify_timing_proof(
            {"preflight_context", "test_focus_context"},
            skip_verification=True,
            _timings=mock_timings,
        )
        assert set(verified) == {"preflight_context", "test_focus_context"}
        assert rejected == []

    @pytest.mark.fast
    def test_mixed_verified_and_rejected(self, mock_timings):
        """Some obligations verified, others rejected."""
        mock_timings.append({"action": "preflight", "elapsed_ms": 100, "error": None})
        verified, rejected = _verify_timing_proof(
            {"preflight_context", "consistency_check_context"},
            _timings=mock_timings,
        )
        assert "preflight_context" in verified
        assert "consistency_check_context" in rejected

    @pytest.mark.fast
    def test_timing_is_only_a_locator_for_promoted_assurance(self):
        from odibi_anchor._dispatcher._gate_wrappers import _attach_assurance_shadow
        from odibi_anchor._utils._session_state import SessionState
        from odibi_anchor.assurance import T1_CONTROLS, build_assurance_plan
        from odibi_anchor.planning._task_profile import normalize_task_profile

        profile = normalize_task_profile(execution_mode="source_change")
        state = SessionState(active_task_profile=profile, task_verification_epoch=1)
        state.active_assurance_plan = build_assurance_plan(profile)
        state.assurance_t1_controls = tuple(  # type: ignore[attr-defined]
            replace(control, disposition="blocking") for control in T1_CONTROLS
        )
        state.intended_pr_paths = ("src/mod.py",)
        result = _attach_assurance_shadow(
            {"passed": True, "overall_pass": True, "status": "pass", "exit_status": 0,
             "summary": "legacy pass", "metrics": {}, "findings": [], "obligations": []},
            session_state=state,
            session_timings=[
                {"action": "task", "passed": True, "error": None},
                {"action": "test", "passed": True, "error": None, "exit_code": 0,
                 "test_target": "tests/"},
            ],
            deferred=False,
            task_scope=SimpleNamespace(
                changed_paths=("src/mod.py",), changed_line_ranges=(),
                provenance={"target_current_sha": "a" * 40},
            ),
        )

        tests = next(
            item for item in result["metrics"]["t1_assurance"]["results"]
            if item["control_id"] == "Anchor-T1-TESTS"
        )
        assert tests["evidence_state"] == "missing"
        assert result["overall_pass"] is False


# ---------------------------------------------------------------------------
# Integration with workflow_gate_context
# ---------------------------------------------------------------------------


class TestGateTimingIntegration:
    """Tests for timing verification integrated into gate."""

    @pytest.mark.fast
    def test_rejected_obligation_shows_in_metrics(self, project_root, mock_timings):
        result = workflow_gate_context(
            project_root,
            actions_taken=["implement", "edit"],
            files_changed=["src/mod.py"],
            obligations_paid=["preflight_context"],
            _timings_override=mock_timings,
            output_format="dict",
        )
        metrics = result["metrics"]
        assert "preflight_context" in metrics["timing_rejected"]
        assert metrics["timing_verified"] == []

    @pytest.mark.fast
    def test_verified_obligation_in_metrics(self, project_root, mock_timings):
        mock_timings.append({"action": "preflight", "elapsed_ms": 100, "error": None})
        result = workflow_gate_context(
            project_root,
            actions_taken=["implement", "edit"],
            files_changed=["src/mod.py"],
            obligations_paid=["preflight_context"],
            _timings_override=mock_timings,
            output_format="dict",
        )
        metrics = result["metrics"]
        assert "preflight_context" in metrics["timing_verified"]
        assert metrics["timing_rejected"] == []

    @pytest.mark.fast
    def test_rejected_obligation_becomes_unpaid(self, project_root, mock_timings):
        """Rejected obligations should appear as unpaid in the gate output."""
        result = workflow_gate_context(
            project_root,
            actions_taken=["implement", "edit"],
            files_changed=["src/mod.py"],
            obligations_paid=["preflight_context"],
            _timings_override=mock_timings,
            output_format="dict",
        )
        # preflight was rejected, so it should still be in unpaid obligations
        unpaid_tools = [o["tool"] for o in result["obligations"]]
        assert "preflight_context" in unpaid_tools

    @pytest.mark.fast
    def test_skip_verification_allows_all(self, project_root, mock_timings):
        result = workflow_gate_context(
            project_root,
            actions_taken=["implement", "edit"],
            files_changed=["src/mod.py"],
            obligations_paid=["preflight_context", "test_focus_context"],
            skip_timing_verification=True,
            _timings_override=mock_timings,
            output_format="dict",
        )
        metrics = result["metrics"]
        assert "preflight_context" in metrics["timing_verified"]
        assert metrics["timing_rejected"] == []

    @pytest.mark.fast
    def test_rejected_shows_in_risks(self, project_root, mock_timings):
        result = workflow_gate_context(
            project_root,
            actions_taken=["implement", "edit"],
            files_changed=["src/mod.py"],
            obligations_paid=["preflight_context"],
            _timings_override=mock_timings,
            output_format="dict",
        )
        risks_text = " ".join(result["risks"])
        assert "NOT verified" in risks_text or "rejected" in risks_text.lower()


# ---------------------------------------------------------------------------
# Learn reminder tests
# ---------------------------------------------------------------------------


class TestLearnReminder:
    """Tests for the auto-learn reminder on gate PASS."""

    @pytest.mark.fast
    def test_learn_reminder_on_pass_with_files(self, project_root, mock_timings):
        """Gate PASS + files_changed -> learn_reminder emitted."""
        mock_timings.append({"action": "preflight", "elapsed_ms": 100, "error": None})
        mock_timings.append({"action": "test", "elapsed_ms": 200, "error": None})
        mock_timings.append({"action": "task", "elapsed_ms": 50, "error": None})
        mock_timings.append({"action": "consistency", "elapsed_ms": 80, "error": None})
        mock_timings.append({"action": "known_bad", "elapsed_ms": 30, "error": None})
        mock_timings.append({"action": "map", "elapsed_ms": 60, "error": None})
        result = workflow_gate_context(
            project_root,
            actions_taken=["task", "implement", "edit"],
            files_changed=["src/mod.py"],
            obligations_paid=[
                "preflight_context", "test_focus_context",
                "task_execution_context", "consistency_check_context",
                "known_bad_change_context", "codebase_map_context",
            ],
            _timings_override=mock_timings,
            output_format="dict",
        )
        # Should have learn_reminder
        assert result.get("learn_reminder") is not None
        assert result["learn_reminder"]["status"] == "BLOCKING"
        assert "event_templates" in result["learn_reminder"]

    @pytest.mark.fast
    def test_no_learn_reminder_when_unpaid(self, project_root, mock_timings):
        """Gate with unpaid obligations -> no learn_reminder."""
        result = workflow_gate_context(
            project_root,
            actions_taken=["implement", "edit"],
            files_changed=["src/mod.py"],
            _timings_override=mock_timings,
            output_format="dict",
        )
        assert result.get("learn_reminder") is None

    @pytest.mark.fast
    def test_blocking_in_suggested_actions(self, project_root, mock_timings):
        """Gate PASS + files -> BLOCKING prefix in suggested_next_actions."""
        mock_timings.append({"action": "preflight", "elapsed_ms": 100, "error": None})
        mock_timings.append({"action": "test", "elapsed_ms": 200, "error": None})
        mock_timings.append({"action": "task", "elapsed_ms": 50, "error": None})
        mock_timings.append({"action": "consistency", "elapsed_ms": 80, "error": None})
        mock_timings.append({"action": "known_bad", "elapsed_ms": 30, "error": None})
        mock_timings.append({"action": "map", "elapsed_ms": 60, "error": None})
        result = workflow_gate_context(
            project_root,
            actions_taken=["task", "implement", "edit"],
            files_changed=["src/mod.py"],
            obligations_paid=[
                "preflight_context", "test_focus_context",
                "task_execution_context", "consistency_check_context",
                "known_bad_change_context", "codebase_map_context",
            ],
            _timings_override=mock_timings,
            output_format="dict",
        )
        blocking_actions = [a for a in result["suggested_next_actions"] if "BLOCKING" in a]
        assert len(blocking_actions) >= 1
        assert "learn" in blocking_actions[0].lower()
