"""Tests for _enforcement.py — pure enforcement decision functions.

Tests verify the decision logic WITHOUT requiring the full boot sequence.
Each function is tested with synthetic timing records and file sets.
"""
import pytest

from odibi_anchor._dispatcher._enforcement import (
    classify_problem_rigor,
    should_block_edit_limit,
    should_block_checkpoint,
    should_block_gate_tests,
    check_test_coverage,
)


@pytest.mark.parametrize(
    ("task", "mode", "priority", "level"),
    [
        ("Fix typo in README", "implementation", "low", 0),
        ("Fix typo in README", "planning", None, 0),
        ("Investigate a localized parser bug", "debugging", "medium", 1),
        ("Plan a cross-system data correctness migration", "migration", "high", 2),
    ],
)
def test_problem_rigor_is_progressive(task, mode, priority, level):
    result = classify_problem_rigor(task, mode=mode, priority=priority)
    assert result["level"] == level
    assert result["record_recommended"] is (level > 0)


# ---------------------------------------------------------------------------
# should_block_edit_limit
# ---------------------------------------------------------------------------

class TestShouldBlockEditLimit:
    """Tests for ungated edit limit enforcement."""

    def test_edit_limit_at_threshold(self):
        """6 edits without gate should block."""
        timings = [
            {"action": "task", "error": None, "passed": True},
            {"action": "touched", "error": None},
            {"action": "touched", "error": None},
            {"action": "touched", "error": None},
            {"action": "safe", "error": None},
            {"action": "semantic", "error": None},
            {"action": "touched", "error": None},
        ]
        blocked, msg = should_block_edit_limit(timings, max_ungated=6)
        assert blocked is True
        assert "6 file edits" in msg

    def test_edit_limit_after_gate_resets(self):
        """Gate resets the counter — edits after gate don't count toward previous."""
        timings = [
            {"action": "touched", "error": None},
            {"action": "touched", "error": None},
            {"action": "touched", "error": None},
            {"action": "gate", "error": None, "passed": True},  # Reset point
            {"action": "touched", "error": None},
            {"action": "touched", "error": None},
        ]
        blocked, msg = should_block_edit_limit(timings, max_ungated=6)
        assert blocked is False
        assert msg == ""

    def test_edit_limit_errors_dont_count(self):
        """Errored edit actions should not count toward the limit."""
        timings = [
            {"action": "touched", "error": None},
            {"action": "touched", "error": "SomeError"},  # This shouldn't count
            {"action": "safe", "error": "AnotherError"},  # This shouldn't count
            {"action": "semantic", "error": None},
            {"action": "touched", "error": None},
        ]
        blocked, msg = should_block_edit_limit(timings, max_ungated=6)
        assert blocked is False  # Only 3 successful edits


# ---------------------------------------------------------------------------
# should_block_checkpoint
# ---------------------------------------------------------------------------

class TestShouldBlockCheckpoint:
    """Tests for checkpoint file threshold enforcement."""

    def test_checkpoint_threshold_exceeded(self):
        """More than threshold files since last checkpoint should block."""
        files_changed = {"a.py", "b.py", "c.py", "d.py", "e.py", "f.py"}
        blocked, msg = should_block_checkpoint(
            files_changed=files_changed,
            files_at_last_checkpoint=0,
            current_touch="g.py",  # New file, not yet in set
            threshold=5,
        )
        assert blocked is True
        assert "7 files changed" in msg

    def test_checkpoint_reconciliation_passes(self):
        """Reconciliation edits should not block."""
        files_changed = {"a.py", "b.py", "c.py", "d.py", "e.py", "f.py"}
        blocked, msg = should_block_checkpoint(
            files_changed=files_changed,
            files_at_last_checkpoint=0,
            current_touch="g.py",
            threshold=5,
            is_reconciliation=True,
        )
        assert blocked is False


# ---------------------------------------------------------------------------
# should_block_gate_tests
# ---------------------------------------------------------------------------

class TestShouldBlockGateReview:
    """Tests for gate review enforcement."""

    def test_blocks_without_review(self):
        """Files changed but no review/diff ran — should block."""
        from odibi_anchor._dispatcher._enforcement import should_block_gate_review
        timings = [
            {"action": "task", "error": None, "passed": True},
            {"action": "touched", "error": None},
        ]
        blocked, msg = should_block_gate_review(timings, {"src/module.py"})
        assert blocked is True
        assert "review" in msg.lower()

    def test_passes_with_review(self):
        """Review ran since last gate — should pass."""
        from odibi_anchor._dispatcher._enforcement import should_block_gate_review
        timings = [
            {"action": "task", "error": None, "passed": True},
            {"action": "touched", "error": None},
            {"action": "review", "error": None},
        ]
        blocked, msg = should_block_gate_review(timings, {"src/module.py"})
        assert blocked is False

    def test_passes_with_diff(self):
        """Diff also satisfies the review requirement."""
        from odibi_anchor._dispatcher._enforcement import should_block_gate_review
        timings = [
            {"action": "touched", "error": None},
            {"action": "diff", "error": None},
        ]
        blocked, msg = should_block_gate_review(timings, {"src/module.py"})
        assert blocked is False

    def test_skipped_when_no_files_changed(self):
        """No files changed — review not required."""
        from odibi_anchor._dispatcher._enforcement import should_block_gate_review
        timings = [{"action": "task", "error": None, "passed": True}]
        blocked, msg = should_block_gate_review(timings, set())
        assert blocked is False

    def test_requires_review_per_gate_cycle(self):
        """Review from a prior gate cycle doesn't count for the current one."""
        from odibi_anchor._dispatcher._enforcement import should_block_gate_review
        timings = [
            {"action": "review", "error": None},
            {"action": "gate", "passed": True},     # gate completed — new cycle
            {"action": "touched", "error": None},    # new edits, no new review
        ]
        blocked, msg = should_block_gate_review(timings, {"src/module.py"})
        assert blocked is True


class TestShouldBlockGateTests:
    """Tests for gate test enforcement."""

    def test_gate_blocks_without_tests(self):
        """.py changes with no test run should block."""
        timings = [
            {"action": "task", "error": None, "passed": True},
            {"action": "touched", "error": None},
        ]
        files_changed = {"src/module.py"}
        blocked, msg = should_block_gate_tests(timings, files_changed)
        assert blocked is True
        assert "Tests required" in msg

    def test_gate_passes_no_py(self):
        """Non-.py changes without tests should not block."""
        timings = [
            {"action": "task", "error": None, "passed": True},
            {"action": "touched", "error": None},
        ]
        files_changed = {"config.toml", "README.md"}
        blocked, msg = should_block_gate_tests(timings, files_changed)
        assert blocked is False


# ---------------------------------------------------------------------------
# check_test_coverage
# ---------------------------------------------------------------------------

class TestCheckTestCoverage:
    """Tests for test coverage cross-reference."""

    def test_coverage_with_test_map(self):
        """Frame test_map should resolve coverage correctly."""
        test_map = {
            "src/foo.py": ["tests/test_foo.py"],
            "src/bar.py": ["tests/test_bar.py"],
        }
        covered, uncovered = check_test_coverage(
            changed_py=["src/foo.py", "src/bar.py"],
            test_target="tests/test_foo.py",
            test_map=test_map,
        )
        # bar.py is mapped to test_bar.py, not test_foo.py — should be uncovered
        assert covered is False
        assert "src/bar.py" in uncovered

    def test_coverage_stem_matching(self):
        """Stem matching should work without test_map."""
        covered, uncovered = check_test_coverage(
            changed_py=["src/odibi_anchor/_dispatcher/_enforcement.py"],
            test_target="tests/_dispatcher/test_enforcement.py",
            test_map=None,
        )
        assert covered is True
        assert uncovered == []


# ---------------------------------------------------------------------------
# should_block_learn_debt
# ---------------------------------------------------------------------------

class TestShouldBlockLearnDebt:
    """Tests for learn debt enforcement."""

    def test_blocks_when_debt_exists(self):
        """Should block when prior session learn debt is True."""
        from odibi_anchor._dispatcher._enforcement import should_block_learn_debt
        blocked, msg = should_block_learn_debt(True)
        assert blocked is True
        assert "learn debt" in msg

    def test_allows_when_no_debt(self):
        """Should not block when no learn debt."""
        from odibi_anchor._dispatcher._enforcement import should_block_learn_debt
        blocked, msg = should_block_learn_debt(False)
        assert blocked is False
        assert msg == ""


# ---------------------------------------------------------------------------
# should_block_config_mutation
# ---------------------------------------------------------------------------

class TestShouldBlockConfigMutation:
    """Tests for config mutation enforcement."""

    def test_blocks_mutation_without_planning(self):
        """Should block config mutation when no task has run."""
        from odibi_anchor._dispatcher._enforcement import should_block_config_mutation
        timings = [{"action": "status", "error": None}]
        blocked, msg = should_block_config_mutation(timings, {"suppress_category"})
        assert blocked is True
        assert "planning" in msg

    def test_allows_mutation_after_planning(self):
        """Should allow config mutation after task has run."""
        from odibi_anchor._dispatcher._enforcement import should_block_config_mutation
        timings = [
            {"action": "status", "error": None},
            {"action": "task", "error": None, "passed": True},
        ]
        blocked, msg = should_block_config_mutation(timings, {"suppress_id"})
        assert blocked is False

    def test_allows_read_only_config(self):
        """Should not block read-only config calls (no mutation kwargs)."""
        from odibi_anchor._dispatcher._enforcement import should_block_config_mutation
        timings = []  # No task ran
        blocked, msg = should_block_config_mutation(timings, {"show_all"})
        assert blocked is False

    def test_blocks_file_override_without_planning(self):
        """file_override is a mutation kwarg — should block."""
        from odibi_anchor._dispatcher._enforcement import should_block_config_mutation
        timings = []
        blocked, msg = should_block_config_mutation(timings, {"file_override"})
        assert blocked is True


# ---------------------------------------------------------------------------
# should_block_task_prerequisites
# ---------------------------------------------------------------------------

class TestShouldBlockTaskPrerequisites:
    """Tests for task prerequisite sequence enforcement."""

    def test_blocks_without_status(self):
        """Should block when status hasn't run."""
        from odibi_anchor._dispatcher._enforcement import should_block_task_prerequisites
        timings = []
        blocked, msg = should_block_task_prerequisites(timings)
        assert blocked is True
        assert "status" in msg

    def test_blocks_without_audit_after_status(self):
        """Task-aware memory is not a pre-task prerequisite."""
        from odibi_anchor._dispatcher._enforcement import should_block_task_prerequisites
        timings = [{"action": "status", "error": None}]
        blocked, msg = should_block_task_prerequisites(timings)
        assert blocked is True
        assert "audit" in msg
        assert "memory" not in msg

    def test_blocks_without_new_session(self):
        """Should block when new_session hasn't run after orientation."""
        from odibi_anchor._dispatcher._enforcement import should_block_task_prerequisites
        timings = [
            {"action": "status", "error": None},
            {"action": "audit_history", "error": None},
        ]
        blocked, msg = should_block_task_prerequisites(timings)
        assert blocked is True
        assert "new_session" in msg

    def test_allows_complete_sequence_without_memory_or_known_bad(self):
        """Task acceptance itself owns bounded memory retrieval."""
        from odibi_anchor._dispatcher._enforcement import should_block_task_prerequisites
        timings = [
            {"action": "status", "error": None},
            {"action": "audit_history", "error": None},
            {"action": "new_session", "error": None},
        ]
        blocked, msg = should_block_task_prerequisites(timings)
        assert blocked is False

    def test_errored_status_doesnt_count(self):
        """Errored status call should not satisfy prerequisite."""
        from odibi_anchor._dispatcher._enforcement import should_block_task_prerequisites
        timings = [{"action": "status", "error": "SomeError"}]
        blocked, msg = should_block_task_prerequisites(timings)
        assert blocked is True


# ---------------------------------------------------------------------------
# should_block_task_inputs
# ---------------------------------------------------------------------------

class TestShouldBlockTaskInputs:
    """Tests for task input validation."""

    def test_blocks_empty_description(self):
        """Should block when description is empty."""
        from odibi_anchor._dispatcher._enforcement import should_block_task_inputs
        blocked, msg = should_block_task_inputs("", "a valid goal string")
        assert blocked is True
        assert "description" in msg

    def test_blocks_short_description(self):
        """Should block when description is < 10 chars."""
        from odibi_anchor._dispatcher._enforcement import should_block_task_inputs
        blocked, msg = should_block_task_inputs("short", "a valid goal string")
        assert blocked is True

    def test_blocks_empty_goal(self):
        """Should block when goal is empty."""
        from odibi_anchor._dispatcher._enforcement import should_block_task_inputs
        blocked, msg = should_block_task_inputs("a valid description", "")
        assert blocked is True
        assert "goal" in msg

    def test_blocks_short_goal(self):
        """Should block when goal is < 10 chars."""
        from odibi_anchor._dispatcher._enforcement import should_block_task_inputs
        blocked, msg = should_block_task_inputs("a valid description", "short")
        assert blocked is True

    def test_allows_valid_inputs(self):
        """Should allow valid description and goal."""
        from odibi_anchor._dispatcher._enforcement import should_block_task_inputs
        blocked, msg = should_block_task_inputs(
            "Extract enforcement logic",
            "Make enforcement testable"
        )
        assert blocked is False
        assert msg == ""


# ---------------------------------------------------------------------------
# should_block_known_bad
# ---------------------------------------------------------------------------

class TestShouldBlockKnownBad:
    """Tests for known_bad prerequisite enforcement."""

    def test_blocks_py_without_known_bad(self):
        """Should block .py file edit without known_bad."""
        from odibi_anchor._dispatcher._enforcement import should_block_known_bad
        timings = [{"action": "status", "error": None}]
        blocked, msg = should_block_known_bad("safe", "src/foo.py", timings)
        assert blocked is True
        assert "known-bad" in msg

    def test_allows_py_after_known_bad(self):
        """Should allow .py file edit after known_bad ran."""
        from odibi_anchor._dispatcher._enforcement import should_block_known_bad
        timings = [{"action": "known_bad", "error": None}]
        blocked, msg = should_block_known_bad("touched", "src/foo.py", timings)
        assert blocked is False

    def test_allows_non_py_without_known_bad(self):
        """Should not block non-.py files."""
        from odibi_anchor._dispatcher._enforcement import should_block_known_bad
        timings = []
        blocked, msg = should_block_known_bad("touched", "README.md", timings)
        assert blocked is False

    def test_allows_empty_target(self):
        """Should not block when target is empty."""
        from odibi_anchor._dispatcher._enforcement import should_block_known_bad
        timings = []
        blocked, msg = should_block_known_bad("safe", "", timings)
        assert blocked is False


# ---------------------------------------------------------------------------
# should_block_planning_gate
# ---------------------------------------------------------------------------

class TestShouldBlockPlanningGate:
    """Tests for planning gate enforcement."""

    def test_blocks_without_status(self):
        """Should block planning-required action without status."""
        from odibi_anchor._dispatcher._enforcement import should_block_planning_gate
        timings = []
        required = {"safe", "semantic", "touched"}
        blocked, msg = should_block_planning_gate("safe", timings, required)
        assert blocked is True
        assert "orientation" in msg

    def test_blocks_without_task(self):
        """Should block when status ran but task didn't."""
        from odibi_anchor._dispatcher._enforcement import should_block_planning_gate
        timings = [{"action": "status", "error": None}]
        required = {"safe", "semantic", "touched"}
        blocked, msg = should_block_planning_gate("safe", timings, required)
        assert blocked is True
        assert "planning" in msg

    def test_allows_after_task(self):
        """Should allow when both status and task have run."""
        from odibi_anchor._dispatcher._enforcement import should_block_planning_gate
        timings = [
            {"action": "status", "error": None},
            {"action": "task", "error": None, "passed": True},
        ]
        required = {"safe", "semantic", "touched"}
        blocked, msg = should_block_planning_gate("safe", timings, required)
        assert blocked is False

    def test_historical_task_timing_cannot_replace_current_task_context(self):
        from odibi_anchor._dispatcher._enforcement import should_block_planning_gate
        timings = [
            {"action": "status", "error": None},
            {"action": "task", "error": None, "passed": True},
        ]
        required = {"safe", "semantic", "touched"}

        blocked, msg = should_block_planning_gate(
            "touched",
            timings,
            required,
            task_context_present=False,
        )

        assert blocked is True
        assert "currently accepted task context" in msg
        assert "Historical task timings do not restore task authority" in msg

    def test_blocks_after_gate_without_new_task(self):
        """After gate passes, new task is required for next feature."""
        from odibi_anchor._dispatcher._enforcement import should_block_planning_gate
        timings = [
            {"action": "status", "error": None},
            {"action": "task", "error": None, "passed": True},
            {"action": "gate", "error": None, "passed": True},
            # No new task after gate
        ]
        required = {"safe", "semantic", "touched"}
        blocked, msg = should_block_planning_gate("safe", timings, required)
        assert blocked is True
        assert "fresh planning" in msg

    def test_allows_new_task_after_gate(self):
        """Should allow when task runs after previous gate."""
        from odibi_anchor._dispatcher._enforcement import should_block_planning_gate
        timings = [
            {"action": "status", "error": None},
            {"action": "task", "error": None, "passed": True},
            {"action": "gate", "error": None, "passed": True},
            {"action": "task", "error": None, "passed": True},  # Fresh planning
        ]
        required = {"safe", "semantic", "touched"}
        blocked, msg = should_block_planning_gate("safe", timings, required)
        assert blocked is False

    def test_ignores_non_required_actions(self):
        """Should not block actions not in planning_required set."""
        from odibi_anchor._dispatcher._enforcement import should_block_planning_gate
        timings = []  # Nothing ran
        required = {"safe", "semantic", "touched"}
        blocked, msg = should_block_planning_gate("status", timings, required)
        assert blocked is False

    def test_failed_task_doesnt_satisfy(self):
        """Task with passed=False should not satisfy planning gate."""
        from odibi_anchor._dispatcher._enforcement import should_block_planning_gate
        timings = [
            {"action": "status", "error": None},
            {"action": "task", "error": None, "passed": False},  # Low readiness
        ]
        required = {"safe", "semantic", "touched"}
        blocked, msg = should_block_planning_gate("safe", timings, required)
        assert blocked is True


# ---------------------------------------------------------------------------
# should_block_gate_learn
# ---------------------------------------------------------------------------

class TestShouldBlockGateLearn:
    """Tests for gate-learn sequencing enforcement."""

    def test_allows_first_gate(self):
        """Should not block the first gate (no prior gate exists)."""
        from odibi_anchor._dispatcher._enforcement import should_block_gate_learn
        timings = [
            {"action": "status", "error": None},
            {"action": "task", "error": None, "passed": True},
        ]
        blocked, msg = should_block_gate_learn(timings)
        assert blocked is False

    def test_blocks_second_gate_without_learn(self):
        """Should block when prior gate passed but learn didn't run."""
        from odibi_anchor._dispatcher._enforcement import should_block_gate_learn
        timings = [
            {"action": "task", "error": None, "passed": True},
            {"action": "gate", "error": None, "passed": True},
            {"action": "task", "error": None, "passed": True},
            # No learn between gates
        ]
        blocked, msg = should_block_gate_learn(timings)
        assert blocked is True
        assert "learn" in msg

    def test_allows_second_gate_with_learn(self):
        """Should allow when learn ran between gates."""
        from odibi_anchor._dispatcher._enforcement import should_block_gate_learn
        timings = [
            {"action": "task", "error": None, "passed": True},
            {"action": "gate", "error": None, "passed": True},
            {"action": "learn", "error": None},
            {"action": "task", "error": None, "passed": True},
        ]
        blocked, msg = should_block_gate_learn(timings)
        assert blocked is False

    def test_failed_gate_doesnt_trigger(self):
        """Failed gate (passed=False) should not create learn obligation."""
        from odibi_anchor._dispatcher._enforcement import should_block_gate_learn
        timings = [
            {"action": "task", "error": None, "passed": True},
            {"action": "gate", "error": None, "passed": False},  # Failed gate
            {"action": "task", "error": None, "passed": True},
        ]
        blocked, msg = should_block_gate_learn(timings)
        assert blocked is False

    def test_successful_checkpoint_carries_its_own_learn(self):
        from odibi_anchor._dispatcher._enforcement import should_block_gate_learn
        timings = [
            {"action": "checkpoint", "error": None, "passed": True, "includes_learn": True},
            {"action": "task", "error": None, "passed": True},
        ]
        blocked, _ = should_block_gate_learn(timings)
        assert blocked is False


class TestModeMismatch:
    def test_read_only_mode_with_detected_files_blocks(self):
        from odibi_anchor._dispatcher._enforcement import should_block_mode_mismatch
        timings = [{"action": "task", "error": None, "task_mode": "analysis"}]
        blocked, message = should_block_mode_mismatch(timings, {"unregistered.py"})
        assert blocked is True
        assert "Mode mismatch" in message

    def test_implementation_mode_with_files_allows(self):
        from odibi_anchor._dispatcher._enforcement import should_block_mode_mismatch
        timings = [{"action": "task", "error": None, "task_mode": "implementation"}]
        blocked, _ = should_block_mode_mismatch(timings, {"changed.py"})
        assert blocked is False
