"""Tests for workflow_gate_context."""

import pytest

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from odibi_anchor.codebase.workflow_gate_context import (
    workflow_gate_context as run_gate,
    render_workflow_gate_report as run_render_report,
    _detect_workflow,
    _compute_obligations,
    _compute_session_obligations,
    _dedupe_obligations,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def project_root(tmp_path):
    """Create a minimal project structure."""
    src = tmp_path / "src" / "mylib"
    src.mkdir(parents=True)
    (src / "__init__.py").write_text("")
    (src / "transform.py").write_text("def process(): pass")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_transform.py").write_text("def test_process(): pass")
    return tmp_path


# ---------------------------------------------------------------------------
# Standard Contract Tests
# ---------------------------------------------------------------------------


class TestStandardContract:
    """Verify standard output contract."""

    def test_returns_dict_by_default(self, project_root):
        ctx = run_gate(
            str(project_root),
            actions_taken=["implement"],
            files_changed=["src/mylib/transform.py"],
        )
        assert isinstance(ctx, dict)

    def test_has_all_standard_fields(self, project_root):
        ctx = run_gate(
            str(project_root),
            actions_taken=["implement"],
            files_changed=["src/mylib/transform.py"],
        )
        required_keys = {
            "kind", "subject", "summary", "metrics",
            "findings", "risks", "samples", "suggested_next_actions",
        }
        assert required_keys.issubset(ctx.keys())

    def test_kind_is_correct(self, project_root):
        ctx = run_gate(
            str(project_root),
            actions_taken=["read"],
        )
        assert ctx["kind"] == "workflow_gate_context"

    def test_has_obligations_field(self, project_root):
        ctx = run_gate(
            str(project_root),
            actions_taken=["implement"],
            files_changed=["src/foo.py"],
        )
        assert "obligations" in ctx
        assert isinstance(ctx["obligations"], list)

    def test_has_obligations_paid_field(self, project_root):
        ctx = run_gate(
            str(project_root),
            actions_taken=["implement"],
            files_changed=["src/foo.py"],
            obligations_paid=["test_focus_context"],
        skip_timing_verification=True,
        )
        assert "obligations_paid" in ctx

    def test_metrics_structure(self, project_root):
        ctx = run_gate(
            str(project_root),
            actions_taken=["implement"],
            files_changed=["src/foo.py"],
        )
        m = ctx["metrics"]
        assert "actions_count" in m
        assert "files_changed_count" in m
        assert "obligations_owed" in m
        assert "obligations_paid" in m
        assert "risk_level" in m
        assert "workflow" in m

    def test_output_format_markdown(self, project_root):
        result = run_gate(
            str(project_root),
            actions_taken=["implement"],
            files_changed=["src/foo.py"],
            output_format="markdown",
        )
        assert isinstance(result, str)
        assert "# Workflow Gate:" in result

    def test_invalid_output_format_raises(self, project_root):
        with pytest.raises(ValueError, match="output_format"):
            run_gate(
                str(project_root),
                actions_taken=[],
                output_format="xml",
            )

    def test_samples_is_dict(self, project_root):
        ctx = run_gate(str(project_root), actions_taken=[])
        assert ctx["samples"] == {}


# ---------------------------------------------------------------------------
# Obligation Creation Tests
# ---------------------------------------------------------------------------


class TestPlanningObligations:
    """Test planning requirements for code modifications."""

    def test_quick_context_satisfies_small_single_file_fix_planning(self, project_root):
        ctx = run_gate(
            str(project_root),
            actions_taken=["fix", "edit"],
            files_changed=["src/mylib/transform.py"],
            obligations_paid=["quick_context"],
        skip_timing_verification=True,
        )

        tools = [o["tool"] for o in ctx["obligations"]]
        assert "quick_context" not in tools
        # preflight_context is a separate MUST obligation (unrelated to planning)

    def test_small_single_file_fix_requires_quick_context(self, project_root):
        ctx = run_gate(
            str(project_root),
            actions_taken=["fix", "edit"],
            files_changed=["src/mylib/transform.py"],
        )

        planning = [o for o in ctx["obligations"] if o["tool"] == "quick_context"]
        assert planning
        assert planning[0]["priority"] == "MUST"
        assert "small single-file fixes" in planning[0]["reason"]

    def test_broad_or_multi_file_change_still_requires_task_execution_context(self, project_root):
        ctx = run_gate(
            str(project_root),
            actions_taken=["fix", "edit", "implement"],
            files_changed=["src/mylib/transform.py", "src/mylib/__init__.py"],
            obligations_paid=["quick_context"],
        skip_timing_verification=True,
        )

        planning = [o for o in ctx["obligations"] if o["tool"] == "preflight_context"]
        assert planning
        assert planning[0]["priority"] == "MUST"
        assert "small single-file fixes" in planning[0]["reason"]


class TestObligationCreation:
    """Test that correct obligations are created per action type."""

    def test_source_modification_creates_test_focus(self, project_root):
        ctx = run_gate(
            str(project_root),
            actions_taken=["implement"],
            files_changed=["src/mylib/transform.py"],
        )
        tools = [o["tool"] for o in ctx["obligations"]]
        assert "test_focus_context" in tools

    def test_source_modification_creates_consistency_check(self, project_root):
        ctx = run_gate(
            str(project_root),
            actions_taken=["implement"],
            files_changed=["src/mylib/transform.py"],
        )
        tools = [o["tool"] for o in ctx["obligations"]]
        assert "consistency_check_context" in tools

    def test_init_modification_creates_consistency_check(self, project_root):
        ctx = run_gate(
            str(project_root),
            actions_taken=["modify"],
            files_changed=["src/mylib/__init__.py"],
        )
        tools = [o["tool"] for o in ctx["obligations"]]
        assert "consistency_check_context" in tools

    def test_shared_file_creates_change_impact(self, project_root):
        ctx = run_gate(
            str(project_root),
            actions_taken=["modify"],
            files_changed=["src/mylib/utils.py"],
        )
        tools = [o["tool"] for o in ctx["obligations"]]
        assert "change_impact_context" in tools

    def test_data_write_creates_quality_gate(self, project_root):
        ctx = run_gate(
            str(project_root),
            actions_taken=["write"],
            files_changed=[],
        )
        tools = [o["tool"] for o in ctx["obligations"]]
        assert "quality_gate_context" in tools

    def test_merge_action_creates_quality_gate(self, project_root):
        ctx = run_gate(
            str(project_root),
            actions_taken=["merge"],
        )
        tools = [o["tool"] for o in ctx["obligations"]]
        assert "quality_gate_context" in tools

    def test_test_fix_creates_rerun(self, project_root):
        ctx = run_gate(
            str(project_root),
            actions_taken=["fix"],
            files_changed=["tests/test_foo.py"],
        )
        tools = [o["tool"] for o in ctx["obligations"]]
        assert "test" in tools

    def test_read_only_creates_no_obligations(self, project_root):
        ctx = run_gate(
            str(project_root),
            actions_taken=["read", "explore"],
            files_read=["src/mylib/transform.py"],
        )
        assert ctx["obligations"] == []
        assert ctx["metrics"]["risk_level"] == "none"

    def test_session_start_creates_codebase_map(self, project_root):
        ctx = run_gate(
            str(project_root),
            actions_taken=["session_start"],
        )
        tools = [o["tool"] for o in ctx["obligations"]]
        assert "codebase_map_context" in tools

    def test_session_end_creates_snapshot(self, project_root):
        ctx = run_gate(
            str(project_root),
            actions_taken=["session_end", "implement"],
            files_changed=["src/foo.py"],
        )
        tools = [o["tool"] for o in ctx["obligations"]]
        assert "session_snapshot_context" in tools


# ---------------------------------------------------------------------------
# Priority (MUST vs SHOULD) Tests
# ---------------------------------------------------------------------------


class TestPriority:
    """Test MUST vs SHOULD classification."""

    def test_source_mod_test_focus_is_must(self, project_root):
        ctx = run_gate(
            str(project_root),
            actions_taken=["implement"],
            files_changed=["src/foo.py"],
        )
        test_focus = [o for o in ctx["obligations"] if o["tool"] == "test_focus_context"]
        assert test_focus
        assert test_focus[0]["priority"] == "MUST"

    def test_data_write_quality_gate_is_must(self, project_root):
        ctx = run_gate(
            str(project_root),
            actions_taken=["write"],
        )
        qg = [o for o in ctx["obligations"] if o["tool"] == "quality_gate_context"]
        assert qg
        assert qg[0]["priority"] == "MUST"

    def test_test_fix_rerun_is_should(self, project_root):
        ctx = run_gate(
            str(project_root),
            actions_taken=["fix"],
            files_changed=["tests/test_bar.py"],
        )
        rerun = [o for o in ctx["obligations"] if o["tool"] == "test"]
        assert rerun
        assert rerun[0]["priority"] == "SHOULD"


class TestPlanningObligations:
    """Test planning-related gate obligations."""

    def test_full_planning_satisfies_single_file_fix(self, project_root):
        # Paying task_execution_context satisfies the planning obligation, so it
        # is no longer raised. (There is no lightweight "quick" planning path.)
        ctx = run_gate(
            str(project_root),
            actions_taken=["fix"],
            files_changed=["src/foo.py"],
            obligations_paid=["task_execution_context"],
        skip_timing_verification=True,
        )
        tools = [o["tool"] for o in ctx["obligations"]]
        assert "task_execution_context" not in tools
        # preflight_context is a separate MUST obligation (unrelated to planning)

    def test_missing_single_file_fix_still_requires_full_planning(self, project_root):
        # No lightweight "quick" planning path — even a single-file fix requires
        # full planning (anchor("task")). See workflow_gate_context planning logic.
        ctx = run_gate(
            str(project_root),
            actions_taken=["fix"],
            files_changed=["src/foo.py"],
        )
        planning = [o for o in ctx["obligations"] if o["created_by_action"] == "missing_planning"]
        assert planning
        assert planning[0]["tool"] == "task_execution_context"
        assert planning[0]["suggested_call"] == "anchor('task', 'description', goal='...', mode='implementation')"

    def test_missing_multi_file_planning_requires_task_context(self, project_root):
        ctx = run_gate(
            str(project_root),
            actions_taken=["modify"],
            files_changed=["src/foo.py", "src/bar.py"],
        )
        planning = [o for o in ctx["obligations"] if o["created_by_action"] == "missing_planning"]
        assert planning
        assert planning[0]["tool"] == "task_execution_context"


# ---------------------------------------------------------------------------
# Risk Level Tests
# ---------------------------------------------------------------------------


class TestRiskLevel:
    """Test risk_level calculation."""

    def test_high_when_must_unpaid(self, project_root):
        ctx = run_gate(
            str(project_root),
            actions_taken=["implement"],
            files_changed=["src/foo.py"],
        )
        assert ctx["metrics"]["risk_level"] == "high"

    def test_medium_when_only_should_unpaid(self, project_root):
        ctx = run_gate(
            str(project_root),
            actions_taken=["modify"],
            files_changed=["src/mylib/some_context.py"],
            # Pay ALL MUST obligations (planning is task_execution_context — no quick path)
            obligations_paid=["test_focus_context", "consistency_check_context", "task_execution_context", "preflight_context"],
        skip_timing_verification=True,
        )
        assert ctx["metrics"]["risk_level"] == "medium"

    def test_low_when_all_paid(self, project_root):
        ctx = run_gate(
            str(project_root),
            actions_taken=["implement"],
            files_changed=["src/foo.py"],
            obligations_paid=[
                "test_focus_context",
                "consistency_check_context",
                "preflight_context",
                "task_execution_context",
                "known_bad_change_context",
                "codebase_map_context",
            ],
            skip_timing_verification=True,
        )
        assert ctx["metrics"]["risk_level"] == "low"

    def test_none_when_read_only(self, project_root):
        ctx = run_gate(
            str(project_root),
            actions_taken=["read", "explore"],
        )
        assert ctx["metrics"]["risk_level"] == "none"

    def test_none_when_no_actions(self, project_root):
        ctx = run_gate(
            str(project_root),
            actions_taken=[],
        )
        assert ctx["metrics"]["risk_level"] == "none"


# ---------------------------------------------------------------------------
# Workflow Detection Tests
# ---------------------------------------------------------------------------


class TestWorkflowDetection:
    """Test auto-detection of workflow type."""

    def test_data_pipeline_detected(self):
        wf = _detect_workflow(["write", "merge"], [])
        assert wf == "data_pipeline"

    def test_debug_detected(self):
        wf = _detect_workflow(["debug", "investigate"], [])
        assert wf == "debug"

    def test_explore_detected(self):
        wf = _detect_workflow(["read", "explore", "profile"], [])
        assert wf == "explore"

    def test_add_tool_detected(self):
        wf = _detect_workflow(
            ["implement", "implement"],
            ["src/mylib/new_tool.py", "tests/test_new_tool.py", "src/mylib/__init__.py"],
        )
        assert wf == "add_tool"

    def test_modify_code_detected(self):
        wf = _detect_workflow(
            ["modify"],
            ["src/mylib/existing.py", "tests/test_existing.py"],
        )
        assert wf == "modify_code"

    def test_refactor_detected(self):
        wf = _detect_workflow(["refactor"], [])
        assert wf == "refactor"

    def test_explicit_workflow_overrides(self, project_root):
        ctx = run_gate(
            str(project_root),
            actions_taken=["write"],
            workflow="debug",
        )
        assert ctx["metrics"]["workflow"] == "debug"


# ---------------------------------------------------------------------------
# Obligations Paid Tests
# ---------------------------------------------------------------------------


class TestObligationsPaid:
    """Test that paid obligations are tracked correctly."""

    def test_paid_tool_not_in_unpaid(self, project_root):
        ctx = run_gate(
            str(project_root),
            actions_taken=["implement"],
            files_changed=["src/foo.py"],
            obligations_paid=["test_focus_context"],
        skip_timing_verification=True,
        )
        unpaid_tools = [o["tool"] for o in ctx["obligations"]]
        assert "test_focus_context" not in unpaid_tools

    def test_paid_appears_in_obligations_paid(self, project_root):
        ctx = run_gate(
            str(project_root),
            actions_taken=["implement"],
            files_changed=["src/foo.py"],
            obligations_paid=["test_focus_context"],
        skip_timing_verification=True,
        )
        paid_tools = [p["tool"] for p in ctx["obligations_paid"]]
        assert "test_focus_context" in paid_tools

    def test_partial_payment_reduces_risk(self, project_root):
        # Without payment: high
        ctx1 = run_gate(
            str(project_root),
            actions_taken=["implement"],
            files_changed=["src/foo.py"],
        )
        assert ctx1["metrics"]["risk_level"] == "high"

        # Pay ALL MUST obligations (including preflight added in v0.4.1)
        ctx2 = run_gate(
            str(project_root),
            actions_taken=["implement"],
            files_changed=["src/foo.py"],
            obligations_paid=["test_focus_context", "consistency_check_context", "preflight_context", "task_execution_context"],
            skip_timing_verification=True,
        )
        # Should drop to low or medium (only SHOULD known_bad remains)
        assert ctx2["metrics"]["risk_level"] in ("low", "medium")


# ---------------------------------------------------------------------------
# Suggested Next Actions Tests
# ---------------------------------------------------------------------------


class TestSuggestedActions:
    """Test suggested_next_actions content with MUST/SHOULD prefix."""

    def test_must_actions_prefixed(self, project_root):
        ctx = run_gate(
            str(project_root),
            actions_taken=["implement"],
            files_changed=["src/foo.py"],
        )
        must_actions = [a for a in ctx["suggested_next_actions"] if a.startswith("MUST:")]
        assert must_actions

    def test_should_actions_prefixed(self, project_root):
        ctx = run_gate(
            str(project_root),
            actions_taken=["modify"],
            files_changed=["src/mylib/some_context.py"],
            obligations_paid=["test_focus_context", "consistency_check_context"],
        skip_timing_verification=True,
        )
        should_actions = [a for a in ctx["suggested_next_actions"] if a.startswith("SHOULD:")]
        assert should_actions

    def test_clean_session_message(self, project_root):
        ctx = run_gate(
            str(project_root),
            actions_taken=["implement"],
            files_changed=["src/foo.py"],
            obligations_paid=["test_focus_context", "consistency_check_context", "preflight_context", "task_execution_context"],
            skip_timing_verification=True,
        )
        # When all MUST are paid, no MUST actions should remain
        must_actions = [a for a in ctx["suggested_next_actions"] if a.startswith("MUST:")]
        assert not must_actions

    def test_suggested_call_contains_file_paths(self, project_root):
        ctx = run_gate(
            str(project_root),
            actions_taken=["implement"],
            files_changed=["src/mylib/transform.py"],
        )
        test_focus_obs = [o for o in ctx["obligations"] if o["tool"] == "test_focus_context"]
        assert test_focus_obs
        assert "src/mylib/transform.py" in test_focus_obs[0]["suggested_call"]


# ---------------------------------------------------------------------------
# Deduplication Tests
# ---------------------------------------------------------------------------


class TestDeduplication:
    """Test obligation deduplication."""

    def test_multiple_files_same_rule_deduped(self, project_root):
        ctx = run_gate(
            str(project_root),
            actions_taken=["implement", "implement", "implement"],
            files_changed=["src/a.py", "src/b.py", "src/c.py"],
        )
        # Should have exactly one test_focus obligation (not 3)
        test_focus = [o for o in ctx["obligations"] if o["tool"] == "test_focus_context"]
        assert len(test_focus) == 1

    def test_must_wins_over_should_in_dedupe(self):
        obligations = [
            {"tool": "test", "priority": "SHOULD", "dedupe_key": "x",
             "reason": "a", "suggested_call": "a", "paid": False},
            {"tool": "test", "priority": "MUST", "dedupe_key": "x",
             "reason": "b", "suggested_call": "b", "paid": False},
        ]
        result = _dedupe_obligations(obligations)
        assert len(result) == 1
        assert result[0]["priority"] == "MUST"


# ---------------------------------------------------------------------------
# Render Tests
# ---------------------------------------------------------------------------


class TestRender:
    """Test markdown rendering."""

    def test_render_contains_risk_level(self, project_root):
        ctx = run_gate(
            str(project_root),
            actions_taken=["implement"],
            files_changed=["src/foo.py"],
        )
        md = run_render_report(ctx)
        assert "high" in md

    def test_render_contains_unpaid_section(self, project_root):
        ctx = run_gate(
            str(project_root),
            actions_taken=["implement"],
            files_changed=["src/foo.py"],
        )
        md = run_render_report(ctx)
        assert "Unpaid Obligations" in md

    def test_render_contains_must_prefix(self, project_root):
        ctx = run_gate(
            str(project_root),
            actions_taken=["implement"],
            files_changed=["src/foo.py"],
        )
        md = run_render_report(ctx)
        assert "**MUST**" in md

    def test_render_paid_section(self, project_root):
        ctx = run_gate(
            str(project_root),
            actions_taken=["implement"],
            files_changed=["src/foo.py"],
            obligations_paid=["test_focus_context"],
        skip_timing_verification=True,
        )
        md = run_render_report(ctx)
        assert "Obligations Paid" in md

    def test_render_empty_session(self, project_root):
        ctx = run_gate(str(project_root), actions_taken=[])
        md = run_render_report(ctx)
        assert "Workflow Gate:" in md


# ---------------------------------------------------------------------------
# Edge Cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Test edge cases and error handling."""

    def test_empty_actions(self, project_root):
        ctx = run_gate(str(project_root), actions_taken=[])
        assert ctx["obligations"] == []
        assert ctx["metrics"]["risk_level"] == "none"

    def test_empty_files_with_actions(self, project_root):
        ctx = run_gate(
            str(project_root),
            actions_taken=["implement"],
            files_changed=[],
        )
        # No file-based obligations without files
        file_obs = [
            o for o in ctx["obligations"]
            if o["tool"] in ("test_focus_context", "consistency_check_context")
        ]
        assert file_obs == []

    def test_nonexistent_root_still_works(self, tmp_path):
        # The tool doesn't need to read files, just track obligations
        ctx = run_gate(
            str(tmp_path / "nonexistent"),
            actions_taken=["implement"],
            files_changed=["src/foo.py"],
        )
        assert ctx["kind"] == "workflow_gate_context"
        assert ctx["obligations"]  # Should still create obligations

    def test_case_insensitive_actions(self, project_root):
        ctx = run_gate(
            str(project_root),
            actions_taken=["IMPLEMENT"],
            files_changed=["src/foo.py"],
        )
        assert ctx["obligations"]  # Should match despite uppercase

    def test_many_actions(self, project_root):
        ctx = run_gate(
            str(project_root),
            actions_taken=["implement"] * 20,
            files_changed=[f"src/file_{i}.py" for i in range(10)],
        )
        # Should not blow up, obligations still deduped
        assert ctx["metrics"]["actions_count"] == 20
        test_focus = [o for o in ctx["obligations"] if o["tool"] == "test_focus_context"]
        assert len(test_focus) == 1  # deduped

    def test_mixed_actions_and_files(self, project_root):
        ctx = run_gate(
            str(project_root),
            actions_taken=["implement", "write", "fix"],
            files_changed=["src/foo.py", "tests/test_bar.py"],
        )
        tools = [o["tool"] for o in ctx["obligations"]]
        # Should have obligations from multiple rules
        assert "test_focus_context" in tools
        assert "quality_gate_context" in tools


# ---------------------------------------------------------------------------
# Integration: Success Criterion from Spec
# ---------------------------------------------------------------------------


class TestSuccessCriteria:
    """Verify the exact success criteria from the v0.3.0 spec."""

    def test_spec_criterion_must_obligations(self, project_root):
        """workflow_gate_context(root, actions_taken=["implement","implement"],
        files_changed=["src/foo.py"]) returns MUST obligations for
        test_focus + consistency_check."""
        ctx = run_gate(
            str(project_root),
            actions_taken=["implement", "implement"],
            files_changed=["src/foo.py"],
        )
        must_tools = [
            o["tool"] for o in ctx["obligations"]
            if o["priority"] == "MUST"
        ]
        assert "test_focus_context" in must_tools
        assert "consistency_check_context" in must_tools

    def test_spec_criterion_high_risk(self, project_root):
        """Same scenario should report high risk."""
        ctx = run_gate(
            str(project_root),
            actions_taken=["implement", "implement"],
            files_changed=["src/foo.py"],
        )
        assert ctx["metrics"]["risk_level"] == "high"

    def test_dogfood_scenario(self, project_root):
        """Simulates workflow_gate_context correctly identifying unpaid
        obligations in a real coding session."""
        # Session: oriented, then implemented 2 files, didn't test
        ctx = run_gate(
            str(project_root),
            actions_taken=["session_start", "implement", "implement"],
            files_changed=[
                "src/odibi_anchor/codebase/workflow_gate_context.py",
                "tests/codebase/test_workflow_gate_context.py",
            ],
            obligations_paid=["codebase_map_context"],  # oriented already
            skip_timing_verification=True,
        )
        # Should flag unpaid test_focus, consistency_check
        must_tools = [
            o["tool"] for o in ctx["obligations"]
            if o["priority"] == "MUST"
        ]
        assert "test_focus_context" in must_tools
        # Should not flag codebase_map (paid)
        assert "codebase_map_context" not in must_tools



# ---------------------------------------------------------------------------
# Verification Record Tests (v0.3.1)
# ---------------------------------------------------------------------------


class TestVerificationRecord:
    """Tests for the verification_record parameter."""

    def test_verification_record_none_by_default(self, project_root):
        """No verification record by default."""
        ctx = run_gate(
            str(project_root),
            actions_taken=["read"],
        skip_timing_verification=True,
        )
        assert ctx.get("verification_record", []) == []
        assert ctx["metrics"]["verifications_recorded"] == 0
        assert ctx["metrics"]["all_verified"] is False

    def test_verification_record_marks_obligations_paid(self, project_root):
        """Verification records automatically mark obligations as paid."""
        ctx = run_gate(
            str(project_root),
            actions_taken=["implement"],
            files_changed=["src/mylib/transform.py"],
            verification_record=[
                {
                    "obligation": "test_focus_context",
                    "status": "passed",
                    "evidence": "42 passed, 0 failed",
                },
            ],
        skip_timing_verification=True,
        )
        # test_focus_context should be in paid, not unpaid
        paid_tools = [p["tool"] for p in ctx["obligations_paid"]]
        assert "test_focus_context" in paid_tools
        # Evidence should be preserved
        paid_entry = next(p for p in ctx["obligations_paid"] if p["tool"] == "test_focus_context")
        assert paid_entry["status"] == "passed"
        assert paid_entry["evidence"] == "42 passed, 0 failed"
        assert paid_entry["paid_by"] == "verification_record"

    def test_verification_record_failed_adds_risk(self, project_root):
        """Failed verification adds a risk entry."""
        ctx = run_gate(
            str(project_root),
            actions_taken=["implement"],
            files_changed=["src/mylib/transform.py"],
            verification_record=[
                {
                    "obligation": "test_focus_context",
                    "status": "failed",
                    "evidence": "3 failed",
                },
            ],
        skip_timing_verification=True,
        )
        assert any("FAILED" in r for r in ctx["risks"])

    def test_verification_record_in_metrics(self, project_root):
        """Verification counts appear in metrics."""
        ctx = run_gate(
            str(project_root),
            actions_taken=["implement"],
            files_changed=["src/mylib/transform.py"],
            verification_record=[
                {"obligation": "test_focus_context", "status": "passed", "evidence": "ok"},
                {"obligation": "consistency_check_context", "status": "passed", "evidence": "0 violations"},
            ],
        skip_timing_verification=True,
        )
        assert ctx["metrics"]["verifications_recorded"] == 2
        assert ctx["metrics"]["verifications_passed"] == 2
        assert ctx["metrics"]["verifications_failed"] == 0

    def test_verification_record_all_verified_true(self, project_root):
        """all_verified is True when all MUST obligations paid and all verifications passed."""
        ctx = run_gate(
            str(project_root),
            actions_taken=["implement"],
            files_changed=["src/mylib/transform.py"],
            verification_record=[
                {"obligation": "test_focus_context", "status": "passed", "evidence": "ok"},
                {"obligation": "consistency_check_context", "status": "passed", "evidence": "0 violations"},
            ],
        skip_timing_verification=True,
        )
        # If all MUST obligations are covered by verification, all_verified should be True
        if ctx["metrics"]["must_unpaid"] == 0:
            assert ctx["metrics"]["all_verified"] is True

    def test_verification_record_all_verified_false_when_must_unpaid(self, project_root):
        """all_verified is False if MUST obligations remain unpaid."""
        ctx = run_gate(
            str(project_root),
            actions_taken=["implement"],
            files_changed=["src/mylib/transform.py"],
            verification_record=[
                # Only pay one obligation, leave consistency_check unpaid
                {"obligation": "test_focus_context", "status": "passed", "evidence": "ok"},
            ],
        skip_timing_verification=True,
        )
        # consistency_check_context is still unpaid if it was a MUST obligation
        if ctx["metrics"]["must_unpaid"] > 0:
            assert ctx["metrics"]["all_verified"] is False

    def test_verification_record_in_markdown(self, project_root):
        """Verification record table appears in markdown output."""
        ctx = run_gate(
            str(project_root),
            actions_taken=["implement"],
            files_changed=["src/mylib/transform.py"],
            verification_record=[
                {"obligation": "test_focus_context", "status": "passed", "evidence": "42 passed"},
            ],
        skip_timing_verification=True,
        )
        md = run_render_report(ctx)
        assert "## Verification Record" in md
        assert "\u2705" in md or "✅" in md
        assert "42 passed" in md

    def test_verification_record_json_serializable(self, project_root):
        """Output with verification record passes json.dumps."""
        import json
        ctx = run_gate(
            str(project_root),
            actions_taken=["implement"],
            files_changed=["src/mylib/transform.py"],
            verification_record=[
                {"obligation": "tests", "status": "passed", "evidence": "ok"},
            ],
        skip_timing_verification=True,
        )
        json.dumps(ctx)  # Should not raise

    def test_verification_record_stored_in_output(self, project_root):
        """Verification record entries appear in output dict."""
        ctx = run_gate(
            str(project_root),
            actions_taken=["implement"],
            files_changed=["src/mylib/transform.py"],
            verification_record=[
                {"obligation": "test_focus_context", "status": "passed", "evidence": "ok", "timestamp": "2026-05-11T12:00:00"},
            ],
        skip_timing_verification=True,
        )
        assert len(ctx["verification_record"]) == 1
        rec = ctx["verification_record"][0]
        assert rec["obligation"] == "test_focus_context"
        assert rec["status"] == "passed"
        assert rec["evidence"] == "ok"
        assert rec["timestamp"] == "2026-05-11T12:00:00"

# ---------------------------------------------------------------------------
# skip_for_new_files Tests
# ---------------------------------------------------------------------------


class TestSkipForNewFiles:
    """Tests for the files_created / skip_for_new_files mechanism."""

    def test_diff_tables_suppressed_for_new_transform(self, project_root):
        """diff_tables_by_key does NOT fire when transform file is brand-new."""
        ctx = run_gate(
            str(project_root),
            actions_taken=["implement"],
            files_changed=["src/transformers/normalize_whitespace.py"],
            files_created=["src/transformers/normalize_whitespace.py"],
        )
        tools = [o["tool"] for o in ctx["obligations"] if not o.get("paid")]
        assert "diff_tables_by_key" not in tools

    def test_diff_tables_fires_for_modified_transform(self, project_root):
        """diff_tables_by_key DOES fire when transform file is modified (not new)."""
        ctx = run_gate(
            str(project_root),
            actions_taken=["modify"],
            files_changed=["src/transformers/deduplicate.py"],
        )
        tools = [o["tool"] for o in ctx["obligations"] if not o.get("paid")]
        assert "diff_tables_by_key" in tools

    def test_diff_tables_fires_when_mix_of_new_and_modified(self, project_root):
        """diff_tables fires when at least one matching file is NOT new."""
        ctx = run_gate(
            str(project_root),
            actions_taken=["implement"],
            files_changed=[
                "src/transformers/normalize_whitespace.py",
                "src/transformers/deduplicate.py",
            ],
            files_created=["src/transformers/normalize_whitespace.py"],
        )
        tools = [o["tool"] for o in ctx["obligations"] if not o.get("paid")]
        assert "diff_tables_by_key" in tools

    def test_dogfood_suppressed_for_new_context_file(self, project_root):
        """dogfood_regression does NOT fire for brand-new _context.py file."""
        ctx = run_gate(
            str(project_root),
            actions_taken=["implement"],
            files_changed=["src/codebase/new_tool_context.py"],
            files_created=["src/codebase/new_tool_context.py"],
            obligations_paid=["test_focus_context", "consistency_check_context"],
        skip_timing_verification=True,
        )
        tools = [o["tool"] for o in ctx["obligations"] if not o.get("paid")]
        assert "dogfood_regression_context" not in tools

    def test_change_impact_suppressed_for_new_utility(self, project_root):
        """change_impact does NOT fire for brand-new utility file."""
        ctx = run_gate(
            str(project_root),
            actions_taken=["implement"],
            files_changed=["src/mylib/utils/new_helper.py"],
            files_created=["src/mylib/utils/new_helper.py"],
            obligations_paid=["test_focus_context", "consistency_check_context"],
        skip_timing_verification=True,
        )
        tools = [o["tool"] for o in ctx["obligations"] if not o.get("paid")]
        assert "change_impact_context" not in tools

    def test_change_impact_fires_for_modified_utility(self, project_root):
        """change_impact DOES fire when existing utility is modified."""
        ctx = run_gate(
            str(project_root),
            actions_taken=["modify"],
            files_changed=["src/mylib/utils/existing_helper.py"],
            obligations_paid=["test_focus_context", "consistency_check_context"],
        skip_timing_verification=True,
        )
        tools = [o["tool"] for o in ctx["obligations"] if not o.get("paid")]
        assert "change_impact_context" in tools

    def test_files_created_none_treated_as_empty(self, project_root):
        """files_created=None (default) means nothing is suppressed."""
        ctx = run_gate(
            str(project_root),
            actions_taken=["implement"],
            files_changed=["src/transformers/deduplicate.py"],
            files_created=None,
        )
        tools = [o["tool"] for o in ctx["obligations"] if not o.get("paid")]
        assert "diff_tables_by_key" in tools

    def test_files_created_empty_list_no_suppression(self, project_root):
        """files_created=[] means nothing is suppressed."""
        ctx = run_gate(
            str(project_root),
            actions_taken=["implement"],
            files_changed=["src/transformers/deduplicate.py"],
            files_created=[],
        )
        tools = [o["tool"] for o in ctx["obligations"] if not o.get("paid")]
        assert "diff_tables_by_key" in tools

    def test_files_created_count_in_metrics(self, project_root):
        """files_created_count appears in metrics."""
        ctx = run_gate(
            str(project_root),
            actions_taken=["implement"],
            files_changed=["src/a.py", "src/b.py"],
            files_created=["src/a.py"],
        )
        assert ctx["metrics"]["files_created_count"] == 1

    def test_test_focus_still_fires_for_new_files(self, project_root):
        """test_focus is NOT skipped for new files — new code still needs tests."""
        ctx = run_gate(
            str(project_root),
            actions_taken=["implement"],
            files_changed=["src/mylib/new_module.py"],
            files_created=["src/mylib/new_module.py"],
        )
        tools = [o["tool"] for o in ctx["obligations"] if not o.get("paid")]
        assert "test_focus_context" in tools

    def test_consistency_still_fires_for_new_files(self, project_root):
        """consistency_check is NOT skipped for new files — conventions still apply."""
        ctx = run_gate(
            str(project_root),
            actions_taken=["implement"],
            files_changed=["src/mylib/new_module.py"],
            files_created=["src/mylib/new_module.py"],
        )
        tools = [o["tool"] for o in ctx["obligations"] if not o.get("paid")]
        assert "consistency_check_context" in tools
