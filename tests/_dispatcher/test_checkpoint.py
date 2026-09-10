"""Tests for odibi_anchor._dispatcher._checkpoint — checkpoint validation logic."""

import pytest
from unittest.mock import MagicMock, patch
from types import SimpleNamespace

from odibi_anchor._dispatcher._checkpoint import checkpoint, format_checkpoint
from odibi_anchor._repository_snapshot import RepositorySnapshot


@pytest.fixture(autouse=True)
def mock_learning_lifecycle():
    """Unit fakes still honor checkpoint's mandatory SQLite lifecycle contract."""
    obligation = {"obligation_id": "lob_checkpoint", "status": "active"}

    def terminal(_obligation_id, **_owner):
        return {**obligation, "status": "legacy_closed"}

    with (
        patch(
            "odibi_anchor.codebase.structured_learning_context.activate_learning_obligation",
            return_value=obligation,
        ),
        patch(
            "odibi_anchor.codebase.structured_learning_context.ensure_learning_obligation",
            return_value={**obligation, "created": False},
        ),
        patch(
            "odibi_anchor.codebase.structured_learning_context.learning_obligation",
            side_effect=terminal,
        ),
    ):
        yield


class TestCheckpointValidation:
    """Tests for checkpoint parameter validation."""

    def _make_cw_fn(self, preflight_ok=True, test_ok=True, gate_ok=True, learn_ok=True):
        """Create a mock anchor() function that returns valid results."""
        def mock_cw(action, *args, **kwargs):
            if action == "preflight":
                return {"metrics": {"errors": 0 if preflight_ok else 3}}
            elif action == "test":
                return {"metrics": {"exit_code": 0 if test_ok else 1, "passed": 10, "failed": 0 if test_ok else 2, "errors": 0}}
            elif action == "gate":
                return {"metrics": {"risk_level": "low" if gate_ok else "high"}}
            elif action == "learn":
                return {"metrics": {"memories_added": 1 if learn_ok else 0}}
            elif action == "learning":
                return {"assessment": {"outcome": kwargs.get("outcome")}}
            return {}
        return mock_cw

    def test_learning_assessment_required_when_files_changed(self):
        """checkpoint() requires an explicit learning disposition after changes."""
        anchor_fn = self._make_cw_fn()
        session_files = {"file1.py"}  # Non-empty = files changed
        session_state = SimpleNamespace(files_at_last_checkpoint=0)
        with pytest.raises(RuntimeError, match="requires learning_assessment"):
            checkpoint(anchor_fn, session_files, session_state, label="test")

    def test_learn_events_not_required_when_no_files(self):
        """checkpoint() succeeds without learn_events if no files changed."""
        anchor_fn = self._make_cw_fn()
        session_files = set()  # Empty = no files changed
        session_state = SimpleNamespace(files_at_last_checkpoint=0)
        # Should not raise
        result = checkpoint(anchor_fn, session_files, session_state, label="test")
        assert isinstance(result, dict)

    def test_skip_test_blocked_for_py_files(self):
        """checkpoint(skip_test=True) raises when .py files were changed."""
        anchor_fn = self._make_cw_fn()
        session_files = {"src/module.py"}
        session_state = SimpleNamespace(files_at_last_checkpoint=0)
        with pytest.raises(RuntimeError, match="skip_test=True.*not allowed"):
            checkpoint(anchor_fn, session_files, session_state,
                      label="test", skip_test=True,
                      learn_events=[{"type": "decision", "detail": "x"}])

    def test_skip_test_allowed_for_non_py_files(self):
        """checkpoint(skip_test=True) is fine when only non-.py files changed."""
        anchor_fn = self._make_cw_fn()
        session_files = {"docs/README.md"}
        session_state = SimpleNamespace(files_at_last_checkpoint=0)
        result = checkpoint(anchor_fn, session_files, session_state,
                          label="test", skip_test=True,
                          learn_events=[{"type": "decision", "detail": "x"}])
        assert isinstance(result, dict)

    def test_label_from_positional_arg(self):
        """Label can be provided as first positional arg."""
        anchor_fn = self._make_cw_fn()
        session_files = set()
        session_state = SimpleNamespace(files_at_last_checkpoint=0)
        result = checkpoint(anchor_fn, session_files, session_state, "my_feature")
        assert result["subject"] == "my_feature"

    def test_label_from_kwarg(self):
        """Label from kwarg works."""
        anchor_fn = self._make_cw_fn()
        session_files = set()
        session_state = SimpleNamespace(files_at_last_checkpoint=0)
        result = checkpoint(anchor_fn, session_files, session_state, label="feat_x")
        assert result["subject"] == "feat_x"

    @pytest.mark.parametrize(("name", "value"), [
        ("final", 1), ("final", "true"),
        ("generate_pr_draft", 0), ("generate_pr_draft", "false"),
    ])
    def test_finality_values_require_exact_booleans(self, name, value):
        state = SimpleNamespace(files_at_last_checkpoint=0)
        with pytest.raises(TypeError):
            checkpoint(self._make_cw_fn(), set(), state, label="strict", **{name: value})

    def test_databricks_final_checkpoint_reports_manual_delivery_without_pr_claims(
        self,
        tmp_path,
    ):
        from odibi_anchor._repository_snapshot import (
            DatabricksGitFolderIdentity,
            capture_databricks_git_folder_task_baseline,
        )

        identity = DatabricksGitFolderIdentity(
            "42",
            "/Users/test@example.invalid/odibi_anchor",
            "main",
            "a" * 40,
            "https://example.invalid/repository.git",
            "gitHub",
        )

        class Provider:
            provider_id = "test.databricks-repos"

            def capture_identity(self, _target):
                return identity

        source = tmp_path / "source.py"
        source.write_text("VALUE = 1\n", encoding="utf-8")
        baseline = capture_databricks_git_folder_task_baseline(
            tmp_path,
            Provider(),
            ["source.py"],
            accept_unknown_git_state=True,
        )
        state = SimpleNamespace(
            files_at_last_checkpoint=0,
            task_repository_baseline=baseline,
            task_repository_write_fingerprints={},
            explicit_pr_draft_requested=None,
            artifact_root=str(tmp_path),
            task_window_id="ltw_databricks_final",
            session_id="session-databricks-final",
            active_project=None,
            learning_obligation_id=None,
        )

        result = checkpoint(
            self._make_cw_fn(),
            {"source.py"},
            state,
            label="databricks-final",
            final=True,
            learn_events=[{"type": "decision", "detail": "manual delivery remains required"}],
        )

        readiness = result["samples"]["pr_readiness"]
        assert result["metrics"]["overall_pass"] is True
        assert "manual Databricks delivery required" in result["summary"]
        assert readiness["capability_status"] == "unavailable"
        assert readiness["ready"] is False
        assert readiness["generated"] is False
        assert readiness["target_ref"] == "unavailable"
        assert readiness["target_sha"] == "unavailable"
        assert readiness["merge_base_sha"] == "unavailable"
        assert readiness["conflict"] == "unavailable"
        assert readiness["repository_capabilities"]["local_worktree_status"] == "unavailable"
        assert readiness["task_scoped_content_changes"] == {}
        assert any("Commit and push manually" in action for action in readiness["manual_actions"])
        assert result["suggested_next_actions"] == readiness["manual_actions"]

    def test_databricks_explicit_pr_draft_request_fails_closed(self, tmp_path):
        from odibi_anchor._repository_snapshot import (
            DatabricksGitFolderIdentity,
            capture_databricks_git_folder_task_baseline,
        )

        identity = DatabricksGitFolderIdentity(
            "42", "/Users/test@example.invalid/odibi_anchor", "main", "a" * 40,
            "https://example.invalid/repository.git", "gitHub",
        )

        class Provider:
            provider_id = "test.databricks-repos"

            def capture_identity(self, _target):
                return identity

        (tmp_path / "source.py").write_text("VALUE = 1\n", encoding="utf-8")
        baseline = capture_databricks_git_folder_task_baseline(
            tmp_path, Provider(), ["source.py"], accept_unknown_git_state=True,
        )
        state = SimpleNamespace(
            files_at_last_checkpoint=0,
            task_repository_baseline=baseline,
            task_repository_write_fingerprints={},
            explicit_pr_draft_requested=None,
            artifact_root=str(tmp_path),
            task_window_id="ltw_databricks_pr",
            session_id="session-databricks-pr",
            active_project=None,
            learning_obligation_id=None,
        )

        result = checkpoint(
            self._make_cw_fn(),
            {"source.py"},
            state,
            label="databricks-pr-draft",
            generate_pr_draft=True,
            learn_events=[{"type": "decision", "detail": "PR claims require unavailable evidence"}],
        )

        assert result["metrics"]["overall_pass"] is False
        assert result["metrics"]["failed_at"] == "pr_draft"
        assert result["metrics"]["pr_draft_path"] is None
        assert result["samples"]["pr_readiness"]["ready"] is False
        assert result["samples"]["pr_readiness"]["generated"] is False

    def test_required_draft_failure_discards_provisional_delivery_state(self, tmp_path):
        timings = [{"action": "task", "passed": True, "error": None}]
        baseline = {"prior.py"}
        frame = SimpleNamespace(code_context=SimpleNamespace(preflight_baseline=set(baseline)))
        state = SimpleNamespace(
            files_at_last_checkpoint=4, checkpoint_in_progress=None,
            task_window_id="ltw_checkpoint", session_id="session-checkpoint",
            active_project=None, learning_obligation_id=None,
            explicit_pr_draft_requested=True, target_root=str(tmp_path), artifact_root=str(tmp_path),
            active_task_profile=None, guidance_attestations=(), intended_pr_paths=(),
            evidence_ledger=[], managed_artifact_ledger=[], repository_snapshot="prior-snapshot",
            repository_pr_config="prior-config",
        )

        def anchor_fn(action, **kwargs):
            timings.append({"action": action, "passed": True, "error": None})
            if action == "preflight":
                frame.code_context.preflight_baseline = {"new.py"}
                return {"metrics": {"errors": 0}}
            if action == "test":
                return {"metrics": {"exit_code": 0, "passed": 1, "failed": 0, "errors": 0}}
            if action == "gate":
                return {"metrics": {"risk_level": "low"}}
            if action == "learn":
                return {"metrics": {"memories_added": 0}}
            raise AssertionError(action)

        snapshot = RepositorySnapshot(
            1, str(tmp_path), "feature", "a" * 40, "main", "b" * 40, "c" * 40,
            f"{'c' * 40}..{'a' * 40}", (), (), (), ("x.py",), (),
            "2026-01-01T00:00:00+00:00", {}, "clear",
        )
        with (
            patch("odibi_anchor._repository_snapshot.capture_repository_snapshot", return_value=snapshot),
            patch("odibi_anchor._pr_readiness.load_pr_config", return_value={
                "schema_version": 1, "provider": "azure_devops", "pr_readiness": "required",
                "default_target_ref": "main", "title_prefixes": ["feat"],
                "work_item_required": False, "live_validation_required": False,
                "deny_globs": ["*.env"], "max_changed_line_length": 120,
            }),
            patch("odibi_anchor._pr_readiness.evaluate_pr_readiness"),
            patch("odibi_anchor._pr_readiness.pr_checks_to_evidence", return_value=()),
            patch("odibi_anchor._pr_readiness.render_pr_draft", side_effect=OSError("disk full")),
        ):
            result = checkpoint(
                anchor_fn, {"x.py"}, state, label="final", final=True,
                learn_events=[{"type": "decision", "detail": "transaction remains atomic"}],
                _session_timings=timings, _session_frame=frame,
            )

        assert result["metrics"]["failed_at"] == "pr_draft"
        assert result["metrics"]["core_steps_succeeded"] is True
        assert timings == [{"action": "task", "passed": True, "error": None}]
        assert frame.code_context.preflight_baseline == baseline
        assert state.files_at_last_checkpoint == 4
        assert state.evidence_ledger == [] and state.managed_artifact_ledger == []
        assert state.repository_snapshot == "prior-snapshot"
        assert state.repository_pr_config == "prior-config"


class TestCheckpointOutputContract:
    """Tests for checkpoint output dict structure."""

    def _make_cw_fn(self):
        def mock_cw(action, *args, **kwargs):
            if action == "preflight":
                return {"metrics": {"errors": 0}}
            elif action == "test":
                return {"metrics": {"exit_code": 0, "passed": 5, "failed": 0, "errors": 0}}
            elif action == "gate":
                return {"metrics": {"risk_level": "low"}}
            elif action == "learn":
                return {"metrics": {"memories_added": 0}}
            elif action == "learning":
                return {"assessment": {"outcome": kwargs.get("outcome")}}
            return {}
        return mock_cw

    def test_output_has_kind(self):
        anchor_fn = self._make_cw_fn()
        result = checkpoint(anchor_fn, set(), SimpleNamespace(files_at_last_checkpoint=0), label="x")
        assert result["kind"] == "checkpoint"

    def test_output_has_metrics(self):
        anchor_fn = self._make_cw_fn()
        result = checkpoint(anchor_fn, set(), SimpleNamespace(files_at_last_checkpoint=0), label="x")
        m = result["metrics"]
        assert "preflight_passed" in m
        assert "tests_passed" in m
        assert "gate_risk" in m
        assert "learnings_saved" in m

    def test_pass_case__overall_pass_true(self):
        anchor_fn = self._make_cw_fn()
        state = SimpleNamespace(
            files_at_last_checkpoint=0, task_window_id="ltw_checkpoint",
            session_id="session-checkpoint", active_project=None,
            learning_obligation_id=None,
        )
        result = checkpoint(anchor_fn, set(), state, label="x")
        assert result["metrics"]["overall_pass"] is True
        assert "PASS" in result["summary"]

    def test_preflight_failure__short_circuits(self):
        def fail_cw(action, **kwargs):
            if action == "preflight":
                return {"metrics": {"errors": 3}}
            return {}
        result = checkpoint(fail_cw, set(), SimpleNamespace(files_at_last_checkpoint=0), label="x")
        assert result["metrics"]["failed_at"] == "preflight"
        assert "FAIL" in result["summary"]

    def test_test_failure_aborts_before_gate_and_learn(self):
        calls = []
        def fail_cw(action, **kwargs):
            calls.append(action)
            if action == "preflight":
                return {"metrics": {"errors": 0}}
            if action == "test":
                return {"metrics": {"exit_code": 1, "failed": 1}}
            raise AssertionError(f"unexpected nested action: {action}")

        state = SimpleNamespace(files_at_last_checkpoint=0, checkpoint_in_progress=None)
        result = checkpoint(fail_cw, set(), state, label="x")

        assert result["metrics"]["failed_at"] == "test"
        assert calls == ["preflight", "test"]
        assert state.checkpoint_in_progress is None

    def test_in_progress_marker_visible_to_nested_learn_and_always_cleared(self):
        state = SimpleNamespace(
            files_at_last_checkpoint=0, checkpoint_in_progress=None,
            task_window_id="ltw_checkpoint", session_id="session-checkpoint",
            active_project=None, learning_obligation_id=None,
        )
        seen = []
        def anchor_fn(action, *args, **kwargs):
            if action == "preflight": return {"metrics": {"errors": 0}}
            if action == "test": return {"metrics": {"exit_code": 0}}
            if action == "gate": return {"metrics": {"risk_level": "low"}}
            if action in {"learn", "learning"}:
                seen.append(dict(state.checkpoint_in_progress))
                return {"metrics": {"memories_added": 0}, "assessment": {}}
        checkpoint(anchor_fn, set(), state, label="visible")
        assert seen[0]["learn_started"] is True
        assert seen[0]["gate_passed"] is True
        assert state.checkpoint_in_progress is None

    def test_marker_cleared_when_validation_raises(self):
        state = SimpleNamespace(files_at_last_checkpoint=0, checkpoint_in_progress=None)
        with pytest.raises(RuntimeError):
            checkpoint(lambda *a, **k: {}, {"x.py"}, state, label="failure")
        assert state.checkpoint_in_progress is None


class TestFormatCheckpoint:
    """Tests for format_checkpoint rendering."""

    def test_dict_format_passthrough(self):
        ctx = {"kind": "checkpoint", "summary": "test"}
        result = format_checkpoint(ctx, "dict")
        assert result == ctx

    def test_markdown_format_returns_string(self):
        ctx = {
            "kind": "checkpoint",
            "subject": "feat",
            "summary": "Checkpoint 'feat': PASS",
            "metrics": {"preflight_passed": True, "tests_passed": True, "gate_risk": "low"},
            "findings": ["✓ All good"],
            "risks": [],
            "suggested_next_actions": [],
        }
        result = format_checkpoint(ctx, "markdown")
        assert isinstance(result, str)
        assert "feat" in result
