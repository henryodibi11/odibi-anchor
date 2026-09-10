"""Tests for inject_session_context (extracted to _task_injection module)."""
from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest

from odibi_anchor._dispatcher._task_injection import inject_session_context


@dataclass
class MockAction:
    tool: str


@dataclass
class MockDataContext:
    tables_profiled: dict = field(default_factory=dict)


@dataclass
class MockCodeContext:
    files_changed: set = field(default_factory=set)


@dataclass
class MockFrame:
    data_context: MockDataContext = field(default_factory=MockDataContext)
    code_context: MockCodeContext = field(default_factory=MockCodeContext)
    actions: list = field(default_factory=list)


class TestInjectSessionContext:
    """Test inject_session_context function."""

    def test_known_facts_injected_from_files_changed(self):
        """known_facts should include changed files when session has them."""
        kwargs = {"goal": "test"}
        files_changed = {"src/foo.py", "src/bar.py"}
        frame = MockFrame()

        result = inject_session_context(kwargs, frame, files_changed, False)

        assert "known_facts" in result
        assert any("Files changed this session" in f for f in result["known_facts"])
        assert "src/bar.py" in str(result["known_facts"])
        assert "src/foo.py" in str(result["known_facts"])

    @pytest.mark.parametrize("scope", [["/Workspace/Users/me/repo/src"], [r"C:\\repo\\src"]])
    def test_repository_scope_explicitly_rejects_absolute_paths(self, scope):
        with pytest.raises(ValueError, match=r"explicitly rejects absolute paths.*\['\.'\].*\['src'\]"):
            inject_session_context({"goal": "test", "repository_scope": scope}, MockFrame(), set(), False)

    @pytest.mark.parametrize("memory_limit", [1, 20])
    def test_memory_limit_is_staged_and_not_forwarded_to_planner(self, memory_limit):
        result = inject_session_context(
            {"goal": "test", "memory_limit": memory_limit}, MockFrame(), set(), False,
        )

        assert result["_task_policy_stage"]["memory_limit"] == memory_limit
        assert "memory_limit" not in {
            key for key in result if not key.startswith("_")
        }

    def test_memory_limit_defaults_to_five(self):
        result = inject_session_context({"goal": "test"}, MockFrame(), set(), False)

        assert result["_task_policy_stage"]["memory_limit"] == 5

    @pytest.mark.parametrize("memory_limit", [True, 1.5, "5", None])
    def test_memory_limit_rejects_non_integer_values(self, memory_limit):
        with pytest.raises(TypeError, match="memory_limit must be an int between 1 and 20"):
            inject_session_context(
                {"goal": "test", "memory_limit": memory_limit}, MockFrame(), set(), False,
            )

    @pytest.mark.parametrize("memory_limit", [0, 21, -1])
    def test_memory_limit_rejects_values_outside_bounds(self, memory_limit):
        with pytest.raises(ValueError, match="memory_limit must be between 1 and 20"):
            inject_session_context(
                {"goal": "test", "memory_limit": memory_limit}, MockFrame(), set(), False,
            )

    def test_risks_injected_from_learn_debt(self):
        """risks should include learn debt warning when debt is active."""
        kwargs = {"goal": "test"}
        frame = MockFrame()

        result = inject_session_context(kwargs, frame, set(), True)

        assert "risks" in result
        assert any("learning assessment debt" in r for r in result["risks"])

    def test_exact_existing_work_item_is_linked_into_task_authority(self, tmp_path):
        artifact_root = tmp_path / "managed"
        problems = artifact_root / "problems"
        (artifact_root / "work_items").mkdir(parents=True)
        problems.mkdir()
        from odibi_anchor._dispatcher._work_item import work_item_action

        created = work_item_action(
            artifact_root, "create", title="Implement", outcome="Ship correction",
            problem_record="PRB-2026-0017", specification="MEMORY_SPEC", output_format="dict",
        )
        assert isinstance(created, dict)

        result = inject_session_context(
            {"goal": "implement", "work_item": created["work_item_id"].lower()},
            MockFrame(), set(), False, problems_dir=problems,
        )

        assert result["_task_policy_stage"]["linked_work_item"] == created["work_item_id"]
        assert f"Linked Work Item: {created['work_item_id']}" in result["known_facts"]
        with pytest.raises(FileNotFoundError, match="Work Item does not exist"):
            inject_session_context(
                {"goal": "implement", "work_item": "WI-2026-9999"},
                MockFrame(), set(), False, problems_dir=problems,
            )

    def test_work_item_cannot_fabricate_unrelated_or_closed_authority(self, tmp_path):
        from odibi_anchor._dispatcher._problem import problem_action
        from odibi_anchor._dispatcher._work_item import work_item_action

        artifact_root = tmp_path / "managed"
        problems = artifact_root / "problems"
        problems.mkdir(parents=True)
        created = work_item_action(
            artifact_root, "create", title="Implement", outcome="Ship correction",
            problem_record="PRB-2026-0017", specification="MEMORY_SPEC", output_format="dict",
        )
        assert isinstance(created, dict)
        work_item_id = created["work_item_id"]
        problem_result = problem_action(
            artifact_root, "create", title="Other problem", output_format="dict",
        )
        assert isinstance(problem_result, dict)
        problem = problem_result["problem_id"]
        with pytest.raises(ValueError, match="different Problem"):
            inject_session_context(
                {"goal": "implement", "problem": problem, "work_item": work_item_id},
                MockFrame(), set(), False, problems_dir=problems,
            )
        work_item_action(
            artifact_root, "close", work_item_id, outcome="completed",
            implementation_disposition="implemented", output_format="dict",
        )
        with pytest.raises(ValueError, match="must be open"):
            inject_session_context(
                {"goal": "implement", "work_item": work_item_id},
                MockFrame(), set(), False, problems_dir=problems,
            )

    def test_user_provided_known_facts_not_overwritten(self):
        """User-supplied known_facts must be preserved (appended to, not replaced)."""
        kwargs = {"goal": "test", "known_facts": ["User fact 1", "User fact 2"]}
        files_changed = {"src/foo.py"}
        frame = MockFrame()

        result = inject_session_context(kwargs, frame, files_changed, False)

        # User facts should be first
        assert result["known_facts"][0] == "User fact 1"
        assert result["known_facts"][1] == "User fact 2"
        # Auto-facts should follow
        assert any("Files changed" in f for f in result["known_facts"])

    def test_empty_session_no_injection(self):
        """Empty session state should produce no injection (except spec warning for implementation mode)."""
        kwargs = {"goal": "test", "mode": "implementation"}
        frame = MockFrame()

        result = inject_session_context(kwargs, frame, set(), False)

        # Progressive classification is always visible, even when it bypasses a record.
        assert any("level 0 (direct)" in fact for fact in result["known_facts"])
        # implementation mode without spec= injects a soft warning
        assert len(result.get("risks", [])) == 1
        assert "No spec linked" in result["risks"][0]
        assert "_already_called" not in result

    def test_already_called_from_frame_actions(self):
        """_already_called should be populated from frame.actions."""
        kwargs = {"goal": "test"}
        frame = MockFrame(actions=[MockAction("explore"), MockAction("profile")])

        result = inject_session_context(kwargs, frame, set(), False)

        assert result["_already_called"] == ["explore", "profile"]

    def test_tables_profiled_injected(self):
        """Profiled tables should appear in known_facts."""
        kwargs = {}
        frame = MockFrame(
            data_context=MockDataContext(tables_profiled={"catalog.schema.table1": {"row_count": 100}})
        )

        result = inject_session_context(kwargs, frame, set(), False)

        assert any("Tables already profiled" in f for f in result["known_facts"])
        assert "catalog.schema.table1" in str(result["known_facts"])

    def test_concise_string_spec_phases_do_not_crash(self, tmp_path):
        (tmp_path / "CONCISE_SPEC.md").write_text("""---
status: in-progress
phases:
  - Stabilize runtime
  - Verify lifecycle
success_criteria: []
files_touched: []
---

# Concise
""", encoding="utf-8")

        result = inject_session_context(
            {"goal": "Continue", "spec": "CONCISE"}, MockFrame(), set(), False,
            specs_dir=tmp_path,
        )

        stage = result["_task_policy_stage"]
        assert stage["active_spec"]["phases"][0] == {
            "name": "Stabilize runtime", "status": "not-started",
        }
        assert stage["phase_count"] == 2

    def test_injects_canonical_specification_disposition(self):
        ordinary = inject_session_context(
            {"goal": "Small fix", "mode": "implementation", "rigor": "direct"},
            MockFrame(), set(), False,
        )
        consequential = inject_session_context(
            {
                "goal": "Schema change", "mode": "implementation",
                "traits": ["schema-change"], "rigor": "direct",
            },
            MockFrame(), set(), False,
        )

        assert ordinary["_specification_disposition"] == "recommended"
        assert consequential["_specification_disposition"] == "required"

    def test_linked_persisted_spec_suppresses_automatic_problem(self, tmp_path):
        (tmp_path / "READY_SPEC.md").write_text("""---
status: ready
phases: []
success_criteria: []
files_touched: []
---

# Ready
""", encoding="utf-8")

        result = inject_session_context(
            {
                "goal": "Implement the approved change", "mode": "implementation",
                "rigor": "full", "spec": "READY_SPEC",
            },
            MockFrame(), set(), False, specs_dir=tmp_path,
            problems_dir=tmp_path.parent / "problems",
        )

        assert result["_task_policy_stage"]["auto_problem"] is None

    def test_task_can_seed_problem_once_and_resume_it(self, tmp_path):
        state = SimpleNamespace(active_problem=None, active_project="queue-automation")
        kwargs = {
            "goal": "Choose the first queue bottleneck to address",
            "background": "Queue jobs exceed the agreed processing window.",
            "create_problem": True,
        }

        first = inject_session_context(
            kwargs,
            MockFrame(),
            set(),
            False,
            problems_dir=tmp_path / "problems",
            session_state=state,
        )
        stage = first["_task_policy_stage"]
        assert state.active_problem is None
        assert list((tmp_path / "problems").glob("*.md")) == []
        assert stage["auto_problem"] is not None
        assert stage["linked_problem"] is None
        assert "create_problem" not in first

    def test_explicit_problem_is_injected_as_compact_context(self, tmp_path):
        from odibi_anchor._dispatcher._problem import problem_action

        created = problem_action(
            tmp_path,
            "create",
            title="Queue latency",
            project_id="queue",
            output_format="dict",
        )
        state = SimpleNamespace(active_problem=None, active_project="queue")

        result = inject_session_context(
            {"goal": "Continue analysis", "problem": created["problem_id"]},
            MockFrame(),
            set(),
            False,
            problems_dir=tmp_path / "problems",
            session_state=state,
        )

        assert state.active_problem is None
        assert result["_task_policy_stage"]["linked_problem"] == created["problem_id"]
        assert any("stage 1/7" in fact for fact in result["known_facts"])
        assert "problem" not in result
