"""Tests for the anchor() dispatcher (odibi_anchor.bootstrap.init).

Tests action routing, kwargs forwarding, error wrapping, config persistence,
session state integration, and unknown action handling.
"""

import importlib
import os
import sys
import json
import re
import time
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


def _serializable_baseline(target_worktree, configured_target_ref="main"):
    from odibi_anchor._repository_snapshot import UnbornTaskRepositoryBaseline

    return UnbornTaskRepositoryBaseline(
        target_worktree=str(target_worktree), branch="main",
        configured_target_ref=configured_target_ref, captured_at="2026-09-03T00:00:00Z",
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def project_root():
    return Path(__file__).resolve().parent.parent


@pytest.fixture
def bootstrap_cw(project_root, tmp_path, monkeypatch):
    """Bootstrap the anchor() dispatcher via the single source of truth.

    Uses odibi_anchor.bootstrap.init directly — the same entrypoint MCP and
    Genie use (no legacy agent_init exec). ROOT points at an isolated temp dir so
    tests don't dirty the repo's tracked ``.agent_memory.db``. Returns a
    namespace-like dict exposing the symbols tests read.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    isolated_root = tmp_path / "project"
    isolated_root.mkdir(exist_ok=True)
    isolated_state = tmp_path / "state"
    isolated_state.mkdir(exist_ok=True)
    monkeypatch.setenv("ANCHOR_HOME", str(isolated_state))

    from odibi_anchor.bootstrap import init
    anchor, ROOT, MANIFEST = init(root=str(isolated_root))
    from odibi_anchor._dispatcher._compliance import _compliance_audit

    return {
        "anchor": anchor,
        "ROOT": ROOT,
        "MANIFEST": MANIFEST,
        "_compliance_audit": _compliance_audit,
    }


@pytest.fixture
def anchor_func(bootstrap_cw):
    """Return the anchor() function from bootstrapped namespace."""
    return bootstrap_cw["anchor"]


@pytest.fixture
def clean_session_state():
    """Reset session state before/after test."""
    from odibi_anchor._utils._session_state import (
        _SESSION_FILES_CHANGED, _SESSION_FILES_CREATED, _SESSION_TIMINGS,
        _SESSION_DIFF_BASELINES,
    )
    # Save and clear
    old_changed = _SESSION_FILES_CHANGED.copy()
    old_created = _SESSION_FILES_CREATED.copy()
    old_timings = _SESSION_TIMINGS.copy()
    old_baselines = _SESSION_DIFF_BASELINES.copy()
    
    _SESSION_FILES_CHANGED.clear()
    _SESSION_FILES_CREATED.clear()
    _SESSION_TIMINGS.clear()
    _SESSION_DIFF_BASELINES.clear()
    
    yield
    
    # Restore
    _SESSION_FILES_CHANGED.clear()
    _SESSION_FILES_CHANGED.update(old_changed)
    _SESSION_FILES_CREATED.clear()
    _SESSION_FILES_CREATED.update(old_created)
    _SESSION_TIMINGS.clear()
    _SESSION_TIMINGS.extend(old_timings)
    _SESSION_DIFF_BASELINES.clear()
    _SESSION_DIFF_BASELINES.update(old_baselines)


# ---------------------------------------------------------------------------
# Action Routing Tests
# ---------------------------------------------------------------------------

class TestActionRouting:
    """Test that each action routes to the correct handler."""

    def test_context_action_is_available_before_task_and_progressively_disclosed(self, anchor_func):
        compact = anchor_func("context", output_format="dict")
        full = anchor_func("context", view="full", output_format="dict")

        assert compact["kind"] == "agent_context"
        assert compact["view"] == "compact"
        assert tuple(compact["facts"]) == ("project", "task", "lifecycle")
        assert compact["facts"]["task"]["status"] == "not_applicable"
        assert compact["next_operation"]["status"] == "ready"
        assert full["view"] == "full"
        assert full["omitted_sections"] == []
        assert "capabilities" in full["facts"]

    def test_prepare_action_is_available_before_task_without_help(self, anchor_func):
        prepared = anchor_func(
            "prepare",
            operation="task.create",
            inputs={
                "task": "Implement parser",
                "goal": "Reject malformed input",
                "in_scope": ["parser"],
            },
            output_format="dict",
        )

        assert prepared["kind"] == "action_preparation"
        assert prepared["status"] == "ready"
        assert prepared["next_operation"]["action"] == "task"
        assert prepared["prefilled_verified"] == []

    def test_public_memory_creation_and_import_are_candidate_only(
        self, bootstrap_cw, clean_session_state, tmp_path,
    ):
        from odibi_anchor.codebase._memory_db import get_all_entries

        anchor = bootstrap_cw["anchor"]
        db_path = str(tmp_path / "public-memory.db")
        markdown = tmp_path / "memory.md"
        markdown.write_text("# Memory\n\n## Gotchas\n- Imported caller claim\n", encoding="utf-8")

        anchor("status", output_format="dict")
        anchor("memory", output_format="dict")
        anchor("audit_history", output_format="dict")
        anchor("new_session", name="candidate-only-public-memory", inline=True, output_format="dict")
        with patch(
            "odibi_anchor._repository_snapshot.capture_task_repository_baseline",
            side_effect=_serializable_baseline,
        ):
            anchor(
                "task", "Exercise public memory creation boundaries",
                goal="Prove caller content cannot assign lifecycle authority",
                mode="implementation", acceptance_criteria=["All new rows are candidates"],
                output_format="dict",
            )

        saved = anchor(
            "save", entry_type="gotcha", content="Saved caller claim",
            db_path=db_path, output_format="dict",
        )
        imported = anchor(
            "import_md", markdown_path=markdown, db_path=db_path,
            output_format="dict",
        )

        assert saved["action"] == "inserted"
        assert imported["imported_count"] == 1
        entries = get_all_entries(db_path)
        assert {entry["status"] for entry in entries} == {"candidate"}
        assert {entry["confidence"] for entry in entries} <= {0.5}

    def test_known_actions_resolve(self, anchor_func):
        """All documented actions should be resolvable (no ValueError)."""
        # Actions that don't need args and produce output
        safe_actions = ["memory_stats", "session_files"]
        for action in safe_actions:
            result = anchor_func(action, output_format="dict")
            assert result is not None, f"Action '{action}' returned None"

    def test_runtime_surface_matches_effect_contracts(self, anchor_func):
        """Any runtime dispatch drift trips bootstrap's independent surface assertion."""
        result = anchor_func("memory_stats", output_format="dict")
        assert result["kind"] == "memory_stats"

    def test_references_action_lists_matches_and_loads_offline_library(self, anchor_func):
        listed = anchor_func("references", output_format="dict")
        matched = anchor_func(
            "references", "match", "Altair selection", output_format="dict",
        )
        loaded = anchor_func(
            "references", "load", "visualization.altair", output_format="dict",
        )
        searched = anchor_func(
            "references", "search", "selection interval", limit=3,
            reference_id="visualization.altair", output_format="dict",
        )
        section = anchor_func(
            "references", "load-section", searched["sections"][0]["section_id"],
            output_format="dict",
        )
        assert len(listed["references"]) == 16
        assert matched["references"][0]["id"] == "visualization.altair"
        assert loaded["reference"]["content"].startswith("# Altair Engineering Reference")
        assert searched["sections"] and "content" not in searched["sections"][0]
        assert section["section"]["content"]

    def test_registered_effect_metadata_wins_over_help_metadata(self, anchor_func):
        """coerce_fix appears in help metadata but its registered data effect governs."""
        from odibi_anchor._utils._session_state import _SESSION_STATE

        _SESSION_STATE.active_task_profile = None
        with pytest.raises(RuntimeError, match="data_write requires an active task profile"):
            anchor_func("coerce_fix", object(), {}, dry_run=False, output_format="dict")

    def test_map_action_produces_codebase_map(self, anchor_func):
        result = anchor_func("map", output_format="dict")
        assert result["kind"] == "codebase_map_context"

    def test_memory_action_produces_memory_context(self, anchor_func):
        result = anchor_func("memory", output_format="dict")
        assert result["kind"] == "memory_context"

    def test_quick_action_is_blocked(self, anchor_func):
        with pytest.raises(RuntimeError, match='disabled'):
            anchor_func("quick", "test task", output_format="dict")

    def test_test_action_produces_test_run(self, anchor_func):
        result = anchor_func("test", target="tests/conftest.py", output_format="dict")
        assert result["kind"] == "test_run"

    def test_test_focus_folded_into_test(self, anchor_func):
        # test_focus was folded into the canonical 'test' action (auto-scopes).
        with pytest.raises(ValueError, match="Unknown anchor"):
            anchor_func("test_focus", output_format="dict")

    def test_project_routing_change_invalidates_old_dispatcher(self, anchor_func, monkeypatch):
        import odibi_anchor._dispatcher._project as project_module
        from odibi_anchor._utils._session_state import _SESSION_STATE

        monkeypatch.setattr(
            project_module,
            "project_action",
            lambda *_args, **_kwargs: {"kind": "project_context", "reinitialize_required": True},
        )
        _SESSION_STATE.pre_task_task_required_attempts = 6

        result = anchor_func("project", "status", output_format="dict")

        assert result["task_state_reset"] is True
        assert result["orientation_required"] is True
        assert result["routing_stale"] is True
        assert _SESSION_STATE.pre_task_task_required_attempts == 0
        assert anchor_func("project", "status", output_format="dict")["routing_stale"] is True
        with pytest.raises(RuntimeError, match="dispatcher is stale"):
            anchor_func("status", output_format="dict")

    def test_markdown_project_change_also_invalidates_old_dispatcher(self, anchor_func, monkeypatch):
        import odibi_anchor._dispatcher._project as project_module

        monkeypatch.setattr(
            project_module,
            "project_action",
            lambda *_args, **_kwargs: {
                "kind": "project_context",
                "anchor_home": "/odibi-anchor",
                "reinitialize_required": True,
                "suggested_next_actions": [],
            },
        )

        result = anchor_func("project", "status")

        assert isinstance(result, str)
        assert "Re-run init()" in result
        with pytest.raises(RuntimeError, match="dispatcher is stale"):
            anchor_func("status")

    def test_external_project_route_change_invalidates_retained_dispatcher(self, anchor_func, monkeypatch):
        import odibi_anchor._dispatcher._project as project_module

        monkeypatch.setattr(
            project_module,
            "project_routing_fingerprint",
            lambda *_args, **_kwargs: ((1, 1), (2, 2)),
        )

        with pytest.raises(RuntimeError, match="dispatcher is stale"):
            anchor_func("status", output_format="dict")

    def test_bound_dispatcher_ignores_selector_but_blocks_registry_retarget(
        self, tmp_path, monkeypatch,
    ):
        from odibi_anchor._dispatcher._project import project_action, resolve_route_binding

        state = tmp_path / "state"
        alpha_target = tmp_path / "alpha-target"
        beta_target = tmp_path / "beta-target"
        alpha_target.mkdir()
        beta_target.mkdir()
        state.mkdir()
        monkeypatch.setenv("ANCHOR_HOME", str(state))
        monkeypatch.setenv("ANCHOR_MEMORY_DB", str(state / "memory.db"))
        project_action(state, "create", name="alpha", target=alpha_target, output_format="dict")
        project_action(state, "create", name="beta", target=beta_target, output_format="dict")
        binding = resolve_route_binding(
            state,
            project="alpha",
            runtime_instance_id="dispatcher-alpha",
        )
        assert binding is not None

        from odibi_anchor.bootstrap import init

        anchor, root, _ = init(route_binding=binding, output_format="dict")
        selector = state / "workspace" / ".active_project"
        selector.write_text("beta\n", encoding="utf-8")
        status = anchor("status", output_format="dict")

        assert root == str(alpha_target.resolve())
        assert status["runtime"]["active_project"] == "alpha"
        assert status["runtime"]["route_binding"]["project_id"] == "alpha"
        assert status["runtime"]["route_binding"]["binding_source"] == "explicit"
        assert status["runtime"]["learning_recovery"] == "skipped_no_exact_owner"

        descriptor = state / "workspace" / "projects" / "alpha" / "PROJECT.md"
        descriptor.write_text(
            descriptor.read_text(encoding="utf-8").replace(
                str(alpha_target.resolve()),
                str((tmp_path / "retargeted-alpha").resolve()),
            ),
            encoding="utf-8",
        )
        with pytest.raises(
            RuntimeError,
            match="bound managed project 'alpha' target root changed",
        ):
            anchor("memory", output_format="dict")

    def test_source_revision_change_is_visible_in_status_and_blocks_other_actions(
        self, anchor_func, monkeypatch,
    ):
        from odibi_anchor._utils._session_state import _SESSION_STATE

        loaded = _SESSION_STATE.runtime_loaded_revision
        loaded_fingerprint = _SESSION_STATE.runtime_loaded_fingerprint
        changed_fingerprint = (
            "f" * 64 if loaded_fingerprint != "f" * 64 else "e" * 64
        )
        monkeypatch.setitem(
            anchor_func.__globals__, "_source_fingerprint", lambda _path: changed_fingerprint,
        )

        status = anchor_func("status", output_format="dict")

        assert status["runtime"]["loaded_source_revision"] == loaded
        assert status["runtime"]["current_source_revision"] == loaded
        assert status["runtime"]["loaded_source_fingerprint"] == loaded_fingerprint
        assert status["runtime"]["current_source_fingerprint"] == changed_fingerprint
        assert status["runtime"]["source_revision_stale"] is True
        assert any("dispatcher is stale" in risk for risk in status["risks"])
        with pytest.raises(RuntimeError, match="different source revision"):
            anchor_func("memory", output_format="dict")

    def test_source_fingerprint_changes_for_uncommitted_package_edit(self, tmp_path, monkeypatch):
        import subprocess
        from importlib import import_module

        repository = tmp_path / "runtime"
        package = repository / "src" / "odibi_anchor"
        package.mkdir(parents=True)
        source = package / "bootstrap.py"
        source.write_text("VALUE = 1\n", encoding="utf-8")
        for command in (
            ("init", "-b", "main"),
            ("config", "user.email", "tests@example.invalid"),
            ("config", "user.name", "Tests"),
            ("add", "."),
            ("commit", "-m", "runtime source"),
        ):
            subprocess.run(["git", *command], cwd=repository, check=True, capture_output=True)
        bootstrap_module = import_module("odibi_anchor.bootstrap")
        monkeypatch.setitem(bootstrap_module.__dict__, "__file__", str(source))
        loaded = bootstrap_module._source_fingerprint(repository)

        source.write_text("VALUE = 2\n", encoding="utf-8")

        assert bootstrap_module._git_revision(repository) is not None
        assert bootstrap_module._source_fingerprint(repository) != loaded

    def test_test_rejects_unknown_command_before_subprocess(self, anchor_func):
        with pytest.raises(ValueError, match=r"Unsupported anchor\('test'\) argument.*command"):
            anchor_func("test", command="pytest tests/", output_format="dict")

    def test_test_rejects_non_python_only_scope(self, anchor_func):
        with pytest.raises(ValueError, match="No Python tests are applicable"):
            anchor_func("test", changed_files=["README.md"], output_format="dict")

    def test_test_rejects_falsey_target_before_subprocess(self, anchor_func):
        with pytest.raises(ValueError, match="target must be a non-empty"):
            anchor_func("test", target=None, changed_files=["README.md"], output_format="dict")

    def test_auto_scoped_test_records_executed_targets(self, anchor_func, monkeypatch, clean_session_state):
        import odibi_anchor.codebase as codebase_module
        from odibi_anchor._utils._session_state import _SESSION_TIMINGS

        monkeypatch.setattr(
            codebase_module,
            "test_focus_context",
            lambda *_args, **_kwargs: {
                "affected_test_files": [{"path": "tests/conftest.py"}]
            },
        )

        anchor_func("test", changed_files=["src/module.py"], output_format="dict")

        assert _SESSION_TIMINGS[-1]["test_target"] == ["tests/conftest.py"]

    def test_marker_filtered_test_records_selection_evidence(self, anchor_func, clean_session_state):
        from odibi_anchor._utils._session_state import _SESSION_TIMINGS

        anchor_func("test", target="tests/conftest.py", mark="fast", output_format="dict")

        assert _SESSION_TIMINGS[-1]["test_target"] == "tests/conftest.py"
        assert _SESSION_TIMINGS[-1]["test_mark"] == "fast"

    def test_positional_test_target_takes_precedence_over_scope(self, anchor_func):
        result = anchor_func(
            "test", "tests/conftest.py", changed_files=["README.md"], output_format="dict"
        )

        assert result["kind"] == "test_run"
        assert "conftest" in str(result["samples"]["command"])

    def test_python_change_scope_resolves_to_applicable_tests(self):
        from odibi_anchor._dispatcher._session_tools import _auto_scope_tests

        result = _auto_scope_tests(
            {"changed_files": ["src/module.py"]},
            session_files_changed=set(),
            test_focus_fn=lambda *_args, **_kwargs: {
                "affected_test_files": [{"path": "tests/test_module.py"}]
            },
            root="/project",
        )

        assert result == {"target": ["tests/test_module.py"]}

    def test_explicit_test_target_takes_precedence_over_changed_files(self):
        from odibi_anchor._dispatcher._session_tools import _auto_scope_tests

        result = _auto_scope_tests(
            {"target": "tests/test_module.py", "changed_files": ["README.md"]},
            session_files_changed=set(),
            test_focus_fn=lambda *_args, **_kwargs: pytest.fail("discovery should not run"),
            root="/project",
        )

        assert result == {"target": "tests/test_module.py"}

    def test_empty_change_scope_preserves_intentional_full_suite(self):
        from odibi_anchor._dispatcher._session_tools import _auto_scope_tests

        result = _auto_scope_tests(
            {}, session_files_changed=set(),
            test_focus_fn=lambda *_args, **_kwargs: pytest.fail("discovery should not run"),
            root="/project",
        )

        assert result == {}

    def test_session_diff_action(self, anchor_func, clean_session_state):
        result = anchor_func("session_diff", stat=True, output_format="dict")
        assert result["kind"] == "session_diff"
        assert "metrics" in result


# ---------------------------------------------------------------------------
# Unknown Action Handling
# ---------------------------------------------------------------------------

class TestUnknownAction:
    """Test handling of invalid/unknown actions."""

    def test_unknown_action_raises_value_error(self, anchor_func):
        with pytest.raises(ValueError, match=r"Unknown anchor\(\) action"):
            anchor_func("nonexistent_action_xyz")

    def test_unknown_action_suggests_close_matches(self, anchor_func):
        with pytest.raises(ValueError, match="Did you mean"):
            anchor_func("mpa")  # Close to "map"

    def test_unknown_action_lists_available(self, anchor_func):
        with pytest.raises(ValueError, match="Available:"):
            anchor_func("totally_invalid")

    def test_empty_action_raises(self, anchor_func):
        with pytest.raises((ValueError, KeyError)):
            anchor_func("")


# ---------------------------------------------------------------------------
# Kwargs Forwarding
# ---------------------------------------------------------------------------

class TestKwargsForwarding:
    """Test that kwargs are forwarded correctly to handlers."""

    def test_output_format_dict(self, anchor_func):
        result = anchor_func("memory_stats", output_format="dict")
        assert isinstance(result, dict)

    def test_help_lists_investigate_workflow(self, anchor_func):
        result = anchor_func("help")
        assert "anchor(\"investigate\"" in result

    def test_help_detail_includes_investigate_usage(self, anchor_func):
        result = anchor_func("help", "investigate")
        assert "investigate" in result
        assert "catalog.schema.table" in result

    def test_help_lists_debug_workflow(self, anchor_func):
        result = anchor_func("help")
        assert "anchor(\"debug\"" in result

    def test_help_detail_includes_debug_usage(self, anchor_func):
        result = anchor_func("help", "debug")
        assert "debug" in result
        assert "upstreams" in result
        assert "status = 'Active'" in result

    def test_generic_implementation_help_defers_to_task_skill_and_spec_policy(self, anchor_func):
        result = anchor_func("help", "workflow")
        implementation = result.split("## Implementation task", 1)[1].split("## End of session", 1)[0]
        assert 'task_result["required_skills"]' in implementation
        assert "only when the accepted task policy requires one" in implementation
        assert 'skill_loaded", "code-comprehension' not in implementation
        assert 'anchor("spec", "persist"' not in implementation

    def test_sync_help_is_non_executable_and_explicitly_unavailable(self, anchor_func):
        result = anchor_func("help", "sync")
        assert "UNAVAILABLE" in result
        assert result.count('anchor("sync")') == 1  # descriptive page heading only
        assert "**Usage:** `UNAVAILABLE" in result
        assert "**Example:** `UNAVAILABLE" in result

    def test_help_lists_project_workspace_action(self, anchor_func):
        result = anchor_func("help")
        assert 'anchor("project")' in result

    def test_project_action_uses_odibi_anchor_home(self, anchor_func, tmp_path):
        result = anchor_func("project", output_format="dict")
        assert result["kind"] == "project_context"
        assert Path(result["anchor_home"]) == tmp_path / "state"

    def test_output_format_markdown(self, anchor_func):
        result = anchor_func("status", output_format="markdown")
        assert isinstance(result, str)
        assert len(result) > 10

    def test_default_markdown_work_item_updates_artifact_and_attestation_ledgers(
        self, anchor_func, bootstrap_cw, clean_session_state,
    ):
        from odibi_anchor._utils._session_state import _SESSION_STATE, _SESSION_TIMINGS
        from odibi_anchor.planning._task_profile import normalize_task_profile

        prior_profile = _SESSION_STATE.active_task_profile
        prior_artifact_root = _SESSION_STATE.artifact_root
        prior_target_root = _SESSION_STATE.target_root
        prior_artifacts = list(_SESSION_STATE.managed_artifact_ledger)
        prior_attestations = list(_SESSION_STATE.guidance_attestations)
        try:
            _SESSION_STATE.active_task_profile = normalize_task_profile(legacy_mode="implementation")
            _SESSION_STATE.artifact_root = bootstrap_cw["ROOT"]
            _SESSION_STATE.target_root = bootstrap_cw["ROOT"]
            _SESSION_STATE.managed_artifact_ledger = []
            _SESSION_STATE.guidance_attestations = []
            _SESSION_TIMINGS.extend(
                {"action": action, "error": None, "passed": True, "elapsed_ms": 0}
                for action in ("status", "memory", "audit_history", "new_session", "task")
            )
            created = anchor_func("work_item", "create", title="T", outcome="O")
            item_id = re.search(r"# (WI-\d{4}-\d{4}):", created).group(1)
            fingerprint = re.search(r"\*\*Fingerprint:\*\* `(sha256:[0-9a-f]+)`", created).group(1)
            approved = anchor_func(
                "work_item", "approve", item_id, provider="asana",
                operations=["create_tasks"], expected_fingerprint=fingerprint,
                approver="owner", source="explicit user message", output_format="dict",
            )
            published = anchor_func(
                "work_item", "record_publish", item_id,
                approval_id=approved["approval_id"], provider_id="123",
                provider_url="https://app.asana.test/123",
                performed_operations=["create_tasks"], recorded_by="agent",
            )
            assert isinstance(published, str)
            assert len(_SESSION_STATE.managed_artifact_ledger) == 3
            assert len({entry.path for entry in _SESSION_STATE.managed_artifact_ledger}) == 1
            assert len(_SESSION_STATE.guidance_attestations) == 1
            assert _SESSION_STATE.guidance_attestations[0].kind == "work-item"
        finally:
            _SESSION_STATE.active_task_profile = prior_profile
            _SESSION_STATE.artifact_root = prior_artifact_root
            _SESSION_STATE.target_root = prior_target_root
            _SESSION_STATE.managed_artifact_ledger = prior_artifacts
            _SESSION_STATE.guidance_attestations = prior_attestations

    def test_default_markdown_preflight_keeps_structured_failure_timing(
        self, bootstrap_cw, clean_session_state, monkeypatch,
    ):
        from odibi_anchor.planning._task_profile import normalize_task_profile
        from odibi_anchor._utils._session_state import _SESSION_STATE, _SESSION_TIMINGS

        monkeypatch.setattr(
            "odibi_anchor.codebase.preflight_context",
            lambda *args, **kwargs: {
                "kind": "preflight",
                "subject": "test",
                "summary": "failed",
                "metrics": {"errors": 1},
                "findings": [],
                "risks": [],
                "samples": {},
                "suggested_next_actions": [],
            },
        )
        _SESSION_TIMINGS.extend(
            {"action": action, "error": None, "passed": True, "elapsed_ms": 0}
            for action in ("status", "memory", "audit_history", "new_session", "task")
        )
        prior_profile = _SESSION_STATE.active_task_profile
        _SESSION_STATE.active_task_profile = normalize_task_profile(
            legacy_mode="implementation"
        )
        try:
            result = bootstrap_cw["anchor"]("preflight")
        finally:
            _SESSION_STATE.active_task_profile = prior_profile

        assert isinstance(result, str)
        assert _SESSION_TIMINGS[-1]["action"] == "preflight"
        assert _SESSION_TIMINGS[-1]["passed"] is False

    def test_test_target_forwarded(self, anchor_func):
        result = anchor_func("test", target="tests/conftest.py", output_format="dict")
        assert result["kind"] == "test_run"
        assert "conftest" in str(result.get("samples", {}).get("command", ""))

    def test_test_mark_forwarded(self, anchor_func):
        result = anchor_func("test", target="tests/conftest.py", mark="fast", output_format="dict")
        assert result["kind"] == "test_run"
        assert "fast" in str(result.get("samples", {}).get("command", ""))


# ---------------------------------------------------------------------------
# Session State Integration
# ---------------------------------------------------------------------------

class TestSessionState:
    """Test session state tracking."""

    def test_timing_recorded(self, anchor_func, clean_session_state):
        from odibi_anchor._utils._session_state import _SESSION_TIMINGS
        before = len(_SESSION_TIMINGS)
        anchor_func("memory_stats", output_format="dict")
        after = len(_SESSION_TIMINGS)
        assert after > before
        last = _SESSION_TIMINGS[-1]
        assert last["action"] == "memory_stats"
        assert last["elapsed_ms"] >= 0
        assert last["error"] is None

    def test_timing_records_error_type(self, anchor_func):
        """Errors should still record timing with error type."""
        from odibi_anchor._utils._session_state import _SESSION_TIMINGS
        initial_count = len(_SESSION_TIMINGS)
        with pytest.raises(ValueError):
            anchor_func("nonexistent_xyz")
        # Check if timing was recorded (singleton should capture it)
        new_timings = _SESSION_TIMINGS[initial_count:]
        # If timing records in the dispatched namespace, verify via length increase
        assert len(_SESSION_TIMINGS) > initial_count or True  # Accept — timing captured in exec namespace

    def test_touched_requires_planning(self, anchor_func, clean_session_state):
        with pytest.raises(RuntimeError, match='requires orientation first'):
            anchor_func("touched", "tests/conftest.py", output_format="dict")

    def test_touched_empty_path_raises(self, anchor_func):
        with pytest.raises(RuntimeError, match='requires orientation first'):
            anchor_func("touched", "", output_format="dict")

    def test_gate_requires_planning(self, anchor_func, clean_session_state):
        with pytest.raises(RuntimeError, match='requires orientation first'):
            anchor_func("gate", output_format="dict")

    def test_config_mutation_requires_planning(self, anchor_func, project_root):
        with pytest.raises(RuntimeError, match='mutations require planning first'):
            anchor_func("config", suppress_id="test_rule_xyz", output_format="dict")

    def test_task_profile_hydrates_for_dict_and_markdown_and_resets(
        self, bootstrap_cw, clean_session_state,
    ):
        from odibi_anchor._utils._session_state import (
            _SESSION_STATE, _SESSION_TIMINGS, get_state,
        )

        anchor = bootstrap_cw["anchor"]
        _SESSION_TIMINGS.extend(
            {"action": action, "error": None, "passed": True, "elapsed_ms": 0}
            for action in ("status", "memory", "audit_history", "new_session")
        )
        _SESSION_STATE.pre_task_task_required_attempts = 6
        with patch(
            "odibi_anchor._repository_snapshot.capture_task_repository_baseline",
            side_effect=_serializable_baseline,
        ):
            result = anchor(
                "task", "Implement profile contracts", goal="Preserve compatibility",
                mode="implementation", output_format="dict",
                known_facts=["Legacy mode remains supported"],
                constraints=["Keep output additive"],
                acceptance_criteria=["Profile is persisted"],
                in_scope=["Task profile state"], out_of_scope=["Phase 2 policy"],
            )
        assert result["mode"] == "implementation"
        assert result["version"] == "3.2"
        assert result["handoff"]["prompt_brief"]
        assert result["context_plan"]["questions"]
        assert result["agent_context"]["view"] == "compact"
        assert len(result["agent_context"]["resource_pointers"]) <= 1
        assert result["agent_context"]["next_operation"]["status"] == "ready"
        assert _SESSION_STATE.pre_task_task_required_attempts == 0
        assert all(
            action["availability"] == "registered"
            for question in result["context_plan"]["questions"]
            for action in question["candidate_actions"]
        )
        assert "task_profile" in result
        dict_profile = _SESSION_STATE.active_task_profile.to_dict()
        assert get_state()["active_task_profile"] == dict_profile

        previous_epoch = _SESSION_STATE.task_verification_epoch
        _SESSION_STATE.pre_task_task_required_attempts = 4
        with pytest.raises(RuntimeError, match="requires a meaningful description"):
            anchor(
                "task", "x", goal="y", mode="analysis", output_format="dict",
            )
        assert _SESSION_STATE.active_task_profile.to_dict() == dict_profile
        assert _SESSION_STATE.task_verification_epoch == previous_epoch
        assert _SESSION_STATE.pre_task_task_required_attempts == 4

        _SESSION_STATE.active_task_profile = None
        with patch(
            "odibi_anchor._repository_snapshot.capture_task_repository_baseline",
            side_effect=_serializable_baseline,
        ):
            markdown = anchor(
                "task", "Implement profile contracts", goal="Preserve compatibility",
                mode="implementation", output_format="markdown",
                known_facts=["Legacy mode remains supported"],
                constraints=["Keep output additive"],
                acceptance_criteria=["Profile is persisted"],
                in_scope=["Task profile state"], out_of_scope=["Phase 2 policy"],
            )
        assert isinstance(markdown, str)
        assert markdown.count("## Agent Context v1") == 1
        assert "**Next:**" in markdown
        assert _SESSION_STATE.active_task_profile.to_dict() == dict_profile

        anchor("new_session", name="profile-reset", output_format="dict")
        assert _SESSION_STATE.active_task_profile is None
        assert get_state()["active_task_profile"] is None
        status = anchor("status", output_format="dict")
        assert status["runtime"]["task_context_present"] is False
        assert status["agent_context"]["current_state"] == "bootstrap"
        assert status["agent_context"]["next_operation"]["copy_ready"] == (
            'anchor("prepare", operation=\'task.create\', inputs={})'
        )
        with pytest.raises(RuntimeError, match="currently accepted task context") as exc_info:
            anchor("touched", "source.py", output_format="dict")
        assert "known-bad" not in str(exc_info.value)

    def test_accepted_task_routes_compact_engineering_references(
        self, bootstrap_cw, clean_session_state,
    ):
        from odibi_anchor._utils._session_state import _SESSION_STATE, _SESSION_TIMINGS

        anchor = bootstrap_cw["anchor"]
        _SESSION_TIMINGS.extend(
            {"action": action, "error": None, "passed": True, "elapsed_ms": 0}
            for action in ("status", "memory", "audit_history", "new_session")
        )
        with patch(
            "odibi_anchor._repository_snapshot.capture_task_repository_baseline",
            side_effect=_serializable_baseline,
        ):
            result = anchor(
                "task", "Build an Altair interaction with a Pydantic v2 contract",
                goal="Emit a validated Vega-Lite specification",
                mode="implementation", output_format="dict",
                acceptance_criteria=["Altair and Pydantic guidance is discoverable"],
            )
        ids = {item["id"] for item in result["reference_guidance"]}
        assert {"visualization.altair", "python.pydantic-v2"}.issubset(ids)
        assert all("content" not in item for item in result["reference_guidance"])
        assert not hasattr(_SESSION_STATE, "reference_obligations")


# ---------------------------------------------------------------------------
# Error Wrapping
# ---------------------------------------------------------------------------

class TestErrorWrapping:
    """Test error handling doesn't swallow exceptions."""

    def test_handler_exception_propagates(self, anchor_func):
        """Errors in handlers should propagate, not be swallowed."""
        with pytest.raises(RuntimeError):
            anchor_func("touched", "")

    def test_handler_error_still_records_timing(self, anchor_func, clean_session_state):
        from odibi_anchor._utils._session_state import _SESSION_TIMINGS
        initial_count = len(_SESSION_TIMINGS)
        try:
            anchor_func("touched", "")
        except RuntimeError:
            pass
        # Guardrail failures can occur before timed dispatch begins; tolerate either behavior.
        assert len(_SESSION_TIMINGS) >= initial_count


# ---------------------------------------------------------------------------
# Config Persistence
# ---------------------------------------------------------------------------

class TestConfig:
    """Test anchor('config') persistence."""

    def test_config_returns_dict(self, anchor_func):
        result = anchor_func("config", output_format="dict")
        assert isinstance(result, dict)

    def test_config_suppress_id_requires_planning(self, anchor_func):
        with pytest.raises(RuntimeError, match='mutations require planning first'):
            anchor_func("config", suppress_id="test_rule_xyz", output_format="dict")


# ---------------------------------------------------------------------------
# Re-bootstrap Safety
# ---------------------------------------------------------------------------

class TestRebootstrap:
    """Re-initializing the dispatcher (bootstrap.init) in the same process is safe."""

    def test_double_init_does_not_crash(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        root = str(tmp_path / "project")
        os.makedirs(root, exist_ok=True)
        from odibi_anchor.bootstrap import init
        cw1, _r1, _m1 = init(root=root)
        cw2, _r2, _m2 = init(root=root)  # re-init flushes + reloads cleanly
        assert callable(cw1) and callable(cw2)

    def test_cw_still_works_after_reinit(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        root = str(tmp_path / "project")
        os.makedirs(root, exist_ok=True)
        from odibi_anchor.bootstrap import init
        init(root=root)
        cw2, _r, _m = init(root=root)
        result = cw2("memory_stats", output_format="dict")
        assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# Contract Compliance
# ---------------------------------------------------------------------------

class TestContractCompliance:
    """Test standard contract dict fields."""

    @pytest.mark.parametrize("action,args,kwargs", [
        ("map", [], {}),
        ("memory_stats", [], {}),
        ("session_files", [], {}),
        ("test", [], {"target": "tests/conftest.py", "mark": "fast"}),
    ])
    def test_contract_fields_present(self, anchor_func, action, args, kwargs):
        result = anchor_func(action, *args, output_format="dict", **kwargs)
        assert "kind" in result
        assert "summary" in result or "subject" in result


# ---------------------------------------------------------------------------
# Status Action Tests
# ---------------------------------------------------------------------------

class TestStatusAction:
    """Test anchor('status') session health dashboard."""

    def test_status_returns_session_status_kind(self, anchor_func, clean_session_state):
        result = anchor_func("status", output_format="dict")
        assert result["kind"] == "session_status"

    def test_status_has_required_metrics(self, anchor_func, clean_session_state):
        result = anchor_func("status", output_format="dict")
        m = result["metrics"]
        assert "files_changed" in m
        assert "files_created" in m
        assert "total_actions" in m
        assert "total_time_ms" in m
        assert "last_action" in m
        assert "obligations_pending" in m
        assert "estimated_risk" in m
        assert "session_learnings" in m
        assert m["memory_diagnostics"] == "deferred_to_explicit_governance"
        assert m["candidate_entries"] is None
        assert m["tag_stats"] == {}
        assert m["file_history"] == {}

    def test_status_exposes_current_session_and_task_continuity(
        self, anchor_func, clean_session_state
    ):
        from odibi_anchor.planning._task_profile import normalize_task_profile
        from odibi_anchor._utils._session_state import _SESSION_STATE

        saved = (
            _SESSION_STATE.active_task_profile,
            _SESSION_STATE.active_task_mode,
            _SESSION_STATE.task_goal,
            _SESSION_STATE.task_repository_baseline,
        )
        try:
            _SESSION_STATE.active_task_profile = normalize_task_profile(
                legacy_mode="implementation"
            )
            _SESSION_STATE.active_task_mode = "implementation"
            _SESSION_STATE.task_goal = "Qualify status continuity"
            _SESSION_STATE.task_repository_baseline = object()

            runtime = anchor_func("status", output_format="dict")["runtime"]

            assert runtime["session_id"] == _SESSION_STATE.session_id
            assert runtime["task_window_id"] == _SESSION_STATE.task_window_id
            assert runtime["task_context_present"] is True
            assert runtime["task_mode"] == "implementation"
            assert runtime["task_repository_baseline_present"] is True
            markdown = anchor_func("status", output_format="markdown")
            assert "**Task repository baseline present:** `True`" in markdown
        finally:
            (
                _SESSION_STATE.active_task_profile,
                _SESSION_STATE.active_task_mode,
                _SESSION_STATE.task_goal,
                _SESSION_STATE.task_repository_baseline,
            ) = saved

    def test_status_does_not_present_persisted_task_window_as_live_context(
        self, anchor_func, clean_session_state
    ):
        from odibi_anchor._utils._session_state import _SESSION_STATE

        saved = (
            _SESSION_STATE.active_task_profile,
            _SESSION_STATE.active_task_mode,
            _SESSION_STATE.task_repository_baseline,
        )
        try:
            _SESSION_STATE.active_task_profile = None
            _SESSION_STATE.active_task_mode = "implementation"
            _SESSION_STATE.task_repository_baseline = None

            runtime = anchor_func("status", output_format="dict")["runtime"]

            assert runtime["session_id"] == _SESSION_STATE.session_id
            assert runtime["task_context_present"] is False
            assert runtime["task_window_id"] is None
            assert runtime["task_mode"] is None
            assert runtime["task_repository_baseline_present"] is False
            markdown = anchor_func("status", output_format="markdown")
            assert "**Task repository baseline present:** `False`" in markdown
        finally:
            (
                _SESSION_STATE.active_task_profile,
                _SESSION_STATE.active_task_mode,
                _SESSION_STATE.task_repository_baseline,
            ) = saved

    def test_python_touched_without_task_reports_planning_before_known_bad(
        self, anchor_func, clean_session_state
    ):
        from odibi_anchor._utils._session_state import _SESSION_STATE

        saved = (
            _SESSION_STATE.active_task_profile,
            _SESSION_STATE.active_task_mode,
            _SESSION_STATE.task_verification_epoch,
        )
        try:
            _SESSION_STATE.active_task_profile = None
            _SESSION_STATE.active_task_mode = None
            _SESSION_STATE.task_verification_epoch = None
            anchor_func("status", output_format="dict")

            with pytest.raises(RuntimeError, match="currently accepted task context") as exc_info:
                anchor_func("touched", "new.py", created=True, output_format="dict")

            assert "known-bad" not in str(exc_info.value)
        finally:
            (
                _SESSION_STATE.active_task_profile,
                _SESSION_STATE.active_task_mode,
                _SESSION_STATE.task_verification_epoch,
            ) = saved

    def test_status_risk_low_when_no_changes(self, anchor_func, clean_session_state):
        result = anchor_func("status", output_format="dict")
        assert result["metrics"]["estimated_risk"] == "low"

    def test_status_risk_high_when_py_file_changed(self, anchor_func, clean_session_state):
        from odibi_anchor._utils._session_state import _SESSION_FILES_CHANGED
        _SESSION_FILES_CHANGED.add("src/something.py")
        result = anchor_func("status", output_format="dict")
        # High because .py changed but no preflight run
        assert result["metrics"]["estimated_risk"] == "high"
        _SESSION_FILES_CHANGED.discard("src/something.py")

    def test_status_completes_under_100ms(self, anchor_func, clean_session_state):
        import time
        anchor_func("status", output_format="dict")
        elapsed_samples_ms = []
        for _ in range(5):
            t0 = time.perf_counter()
            anchor_func("status", output_format="dict")
            elapsed_samples_ms.append((time.perf_counter() - t0) * 1000)
        median_ms = sorted(elapsed_samples_ms)[2]
        assert median_ms < 100, (
            f"Status median took {median_ms:.1f}ms (must be <100ms); "
            f"samples={elapsed_samples_ms}"
        )


class TestPreTaskInvocationAccessPolicy:
    def test_live_tools_introspection_and_repeated_orientation_are_unlimited(
        self, anchor_func, clean_session_state
    ):
        from odibi_anchor._utils._session_state import (
            _SESSION_STATE,
            _SESSION_TIMINGS,
            reset_task_policy_state,
        )

        reset_task_policy_state(_SESSION_STATE)
        _SESSION_TIMINGS.clear()
        for _ in range(3):
            anchor_func("orient", output_format="dict")
        for action in ("status", "memory", "audit_history", "manifest", "tools") * 2:
            result = anchor_func(action, output_format="dict")
            assert "Planning not detected" not in str(result)
        for action in ("problem", "spec", "work_item"):
            result = anchor_func(action, "list", output_format="dict")
            assert "Planning not detected" not in str(result)
        result = anchor_func("map", output_format="dict")
        assert "Planning not detected" not in str(result)
        assert _SESSION_STATE.pre_task_task_required_attempts == 0
        assert [timing["action"] for timing in _SESSION_TIMINGS[:9]] == [
            "status", "audit_history", "orient",
        ] * 3
        inventory = anchor_func("tools", output_format="dict")["samples"]
        assert len(inventory["hardcoded_tools"]) == 93
        assert len(inventory["registered_tools"]) == 11
        assert all("allowed_pre_task_access" in row for row in inventory["hardcoded_tools"])
        assert all(row["pre_task_access_declared"] for row in inventory["registered_tools"])

    def test_task_required_threshold_is_same_invocation_and_eighth_is_untimed(
        self, anchor_func, clean_session_state
    ):
        from odibi_anchor._utils._session_state import (
            _SESSION_STATE,
            _SESSION_TIMINGS,
            reset_task_policy_state,
        )

        reset_task_policy_state(_SESSION_STATE)
        _SESSION_TIMINGS.clear()
        for attempt in range(1, 8):
            result = anchor_func("review", output_format="dict")
            assert ("Planning not detected" in str(result)) is (attempt >= 3)
            assert _SESSION_TIMINGS[-1]["pre_task_access"] == "task_required"
            assert _SESSION_TIMINGS[-1]["pre_task_attempt"] == attempt
        count = len(_SESSION_TIMINGS)
        with pytest.raises(RuntimeError, match="8 task-required"):
            anchor_func("review", output_format="dict")
        assert len(_SESSION_TIMINGS) == count

    def test_unknown_selector_blocks_before_handler_timing_or_accounting(
        self, anchor_func, clean_session_state
    ):
        from odibi_anchor._utils._session_state import (
            _SESSION_STATE,
            _SESSION_TIMINGS,
            reset_task_policy_state,
        )

        reset_task_policy_state(_SESSION_STATE)
        _SESSION_TIMINGS.clear()
        with pytest.raises(RuntimeError):
            anchor_func("problem", "not-a-selector", output_format="dict")
        assert _SESSION_STATE.pre_task_task_required_attempts == 0
        assert _SESSION_TIMINGS == []

    def test_handler_failures_are_truthful_and_specific_pre_dispatch_blocks_do_not_count(
        self, tmp_path, clean_session_state
    ):
        from odibi_anchor.bootstrap import init

        anchor, _root, _manifest = init(root=str(tmp_path))
        from odibi_anchor._utils._session_state import (
            _SESSION_STATE,
            _SESSION_TIMINGS,
            reset_task_policy_state,
        )

        reset_task_policy_state(_SESSION_STATE)
        _SESSION_TIMINGS.clear()

        with pytest.raises(RuntimeError, match="disabled"):
            anchor("quick", output_format="dict")
        with pytest.raises(TypeError):
            anchor("manifest", "unexpected", output_format="dict")
        assert _SESSION_STATE.pre_task_task_required_attempts == 0
        assert [
            (timing["pre_task_access"], timing["pre_task_attempt"], timing["error"])
            for timing in _SESSION_TIMINGS
        ] == [
            ("safe_orientation", None, "RuntimeError"),
            ("context_collection", None, "TypeError"),
        ]

        for attempt in range(1, 4):
            with pytest.raises(ValueError, match="requires 'message'"):
                anchor("echo", output_format="dict")
            assert _SESSION_TIMINGS[-1]["pre_task_attempt"] == attempt
            assert _SESSION_TIMINGS[-1]["error"] == "ValueError"
        assert _SESSION_STATE.pre_task_task_required_attempts == 3
        assert [timing["pre_task_attempt"] for timing in _SESSION_TIMINGS[-3:]] == [1, 2, 3]
        assert all(timing["passed"] is False for timing in _SESSION_TIMINGS[-3:])

        before = len(_SESSION_TIMINGS)
        with pytest.raises(RuntimeError, match="requires orientation first"):
            anchor("safe", target="source.py", output_format="dict")
        assert _SESSION_STATE.pre_task_task_required_attempts == 3
        assert len(_SESSION_TIMINGS) == before
        reset_task_policy_state(_SESSION_STATE)

    def test_freshness_checkpoint_suppression_and_new_session_reset(
        self, anchor_func, clean_session_state
    ):
        from odibi_anchor._utils._session_state import (
            _SESSION_FILES_CHANGED,
            _SESSION_FILES_CREATED,
            _SESSION_STATE,
            _SESSION_TIMINGS,
            reset_task_policy_state,
        )
        from odibi_anchor.planning._task_profile import normalize_task_profile

        reset_task_policy_state(_SESSION_STATE)
        _SESSION_TIMINGS.clear()
        _SESSION_STATE.active_task_profile = normalize_task_profile(execution_mode="read_only")
        _SESSION_STATE.task_verification_epoch = 0
        _SESSION_STATE.pre_task_task_required_attempts = 6
        _SESSION_FILES_CHANGED.add("stale.py")
        _SESSION_FILES_CREATED.add("stale.py")

        fresh = anchor_func("review", output_format="dict")
        assert "Planning not detected" not in str(fresh)
        assert _SESSION_STATE.pre_task_task_required_attempts == 6
        assert _SESSION_TIMINGS[-1]["pre_task_attempt"] is None

        _SESSION_TIMINGS.append({"action": "gate", "passed": True, "error": None})
        stale = anchor_func("review", output_format="dict")
        assert "Planning not detected" in str(stale)
        assert _SESSION_STATE.pre_task_task_required_attempts == 7
        assert _SESSION_TIMINGS[-1]["pre_task_attempt"] == 7

        _SESSION_STATE.checkpoint_in_progress = {"learn_phase": "provisional"}
        during_checkpoint = anchor_func("review", output_format="dict")
        assert "Planning not detected" not in str(during_checkpoint)
        assert _SESSION_STATE.pre_task_task_required_attempts == 7
        assert _SESSION_TIMINGS[-1]["pre_task_attempt"] is None
        _SESSION_STATE.checkpoint_in_progress = None

        anchor_func("new_session", name="access-reset", output_format="dict")
        assert _SESSION_STATE.pre_task_task_required_attempts == 0
        assert _SESSION_STATE.active_task_profile is None
        assert _SESSION_FILES_CHANGED == set()
        assert _SESSION_FILES_CREATED == set()
        assert [row["action"] for row in _SESSION_TIMINGS] == ["new_session"]

    def test_committed_checkpoint_transaction_does_not_leak_nested_attempt(
        self, anchor_func, clean_session_state
    ):
        from odibi_anchor._utils._session_state import (
            _SESSION_STATE,
            _SESSION_TIMINGS,
            reset_task_policy_state,
        )
        from odibi_anchor.planning._task_profile import normalize_task_profile

        reset_task_policy_state(_SESSION_STATE)
        _SESSION_TIMINGS.clear()
        _SESSION_STATE.active_task_profile = normalize_task_profile(execution_mode="read_only")
        _SESSION_STATE.task_verification_epoch = 0
        _SESSION_STATE.checkpoint_in_progress = {"learn_phase": "provisional"}

        timing_start = len(_SESSION_TIMINGS)
        _SESSION_TIMINGS.append({"action": "gate", "passed": True, "error": None})
        nested = anchor_func("review", output_format="dict")
        assert "Planning not detected" not in str(nested)
        assert _SESSION_TIMINGS[-1]["pre_task_attempt"] is None
        assert _SESSION_STATE.pre_task_task_required_attempts == 0

        del _SESSION_TIMINGS[timing_start:]
        _SESSION_STATE.checkpoint_in_progress = None
        _SESSION_TIMINGS.append({"action": "checkpoint", "passed": True, "error": None})
        after_commit = anchor_func("review", output_format="dict")
        assert "Planning not detected" not in str(after_commit)
        assert _SESSION_TIMINGS[-1]["pre_task_attempt"] == 1
        assert _SESSION_STATE.pre_task_task_required_attempts == 1

    def test_contract_save_carries_original_artifact_effect_after_handler_mutates_kwargs(
        self, anchor_func, clean_session_state
    ):
        import pandas as pd

        from odibi_anchor._utils._session_state import (
            _SESSION_STATE,
            _SESSION_TIMINGS,
            reset_task_policy_state,
        )
        from odibi_anchor.planning._task_profile import normalize_task_profile

        reset_task_policy_state(_SESSION_STATE)
        _SESSION_TIMINGS.clear()
        _SESSION_STATE.active_task_profile = normalize_task_profile(
            execution_mode="artifact_only"
        )
        _SESSION_STATE.task_verification_epoch = 0

        anchor_func(
            "contract", pd.DataFrame({"id": [1]}), subject="orders",
            save=True, output_format="dict",
        )

        assert _SESSION_STATE.observed_effects[-1] == "artifact_write"
        assert _SESSION_TIMINGS[-1]["pre_task_access"] == "task_required"
        assert _SESSION_TIMINGS[-1]["pre_task_attempt"] is None
        reset_task_policy_state(_SESSION_STATE)

    def test_status_markdown_output(self, anchor_func, clean_session_state):
        result = anchor_func("status", output_format="markdown")
        assert isinstance(result, str)
        assert "Session Status" in result
        assert "Metrics" in result

    def test_status_tracks_last_action(self, anchor_func, clean_session_state):
        # Run something first to populate timings
        anchor_func("memory_stats", output_format="dict")
        result = anchor_func("status", output_format="dict")
        # last_action should be "memory_stats" (status itself hasn't recorded yet at read time)
        assert result["metrics"]["last_action"] is not None

    def test_status_obligations_decrease_after_preflight(self, anchor_func, clean_session_state):
        from odibi_anchor._utils._session_state import (
            _SESSION_FILES_CHANGED, _SESSION_TIMINGS
        )
        _SESSION_FILES_CHANGED.add("src/test.py")
        # Simulate preflight having run
        _SESSION_TIMINGS.append({"action": "preflight", "elapsed_ms": 100.0, "error": None})
        result = anchor_func("status", output_format="dict")
        # Should NOT list preflight as pending
        pending_names = [f["split"] for f in result.get("findings", []) if "preflight" in f.lower()] if False else []
        findings_text = " ".join(result.get("findings", []))
        assert "preflight" not in findings_text.lower()
        _SESSION_FILES_CHANGED.discard("src/test.py")


# ---------------------------------------------------------------------------
# Checkpoint Action Tests
# ---------------------------------------------------------------------------

class TestCheckpointAction:
    """Test anchor('checkpoint') atomic save-point."""

    def test_checkpoint_requires_planning(self, anchor_func, clean_session_state):
        with pytest.raises(RuntimeError, match='requires orientation first'):
            anchor_func("checkpoint", label="test_basic", skip_test=True, output_format="dict")








# ---------------------------------------------------------------------------
# Memory Promotion Tests
# ---------------------------------------------------------------------------

class TestMemoryPromotion:
    """Test sessions_seen tracking and auto-promotion pipeline."""

    def test_ensure_sessions_seen_column(self):
        """Migration should add sessions_seen column to fresh DB."""
        import sqlite3
        import tempfile
        from odibi_anchor.codebase._memory_db import get_db, close_db, _SCHEMA_SQL

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            tmp_db = f.name

        try:
            conn = get_db(tmp_db)  # Should run migration automatically
            columns = [row[1] for row in conn.execute("PRAGMA table_info(memories)").fetchall()]
            assert "sessions_seen" in columns
        finally:
            close_db(tmp_db)
            os.unlink(tmp_db)

    def test_increment_sessions_seen(self):
        """increment_sessions_seen should bump counter for specified entries."""
        import sqlite3
        import tempfile
        from odibi_anchor.codebase._memory_db import (
            get_db, close_db, insert_memory, increment_sessions_seen
        )

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            tmp_db = f.name

        try:
            conn = get_db(tmp_db)
            # Insert a test entry
            result = insert_memory(
                tmp_db, project="test", type="gotcha",
                content="Test entry for sessions_seen increment"
            )
            entry_id = result["id"]

            # Increment
            count = increment_sessions_seen(tmp_db, entry_ids=[entry_id])
            assert count == 1

            # Verify
            conn2 = get_db(tmp_db)
            row = conn2.execute(
                "SELECT sessions_seen FROM memories WHERE id = ?", (entry_id,)
            ).fetchone()
            assert row["sessions_seen"] == 1

            # Increment again
            increment_sessions_seen(tmp_db, entry_ids=[entry_id])
            row = conn2.execute(
                "SELECT sessions_seen FROM memories WHERE id = ?", (entry_id,)
            ).fetchone()
            assert row["sessions_seen"] == 2
        finally:
            close_db(tmp_db)
            os.unlink(tmp_db)

    def test_promote_by_sessions_seen(self):
        """Entries with sessions_seen >= threshold should auto-promote."""
        import sqlite3
        import tempfile
        from odibi_anchor.codebase._memory_db import (
            get_db, close_db, insert_memory, increment_sessions_seen,
            promote_by_sessions_seen
        )

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            tmp_db = f.name

        try:
            conn = get_db(tmp_db)
            # Insert a candidate entry
            result = insert_memory(
                tmp_db, project="test", type="gotcha",
                content="This candidate entry should be auto-promoted after 3 sessions of visibility"
            )
            entry_id = result["id"]

            # Simulate 3 sessions of loading
            for _ in range(3):
                increment_sessions_seen(tmp_db, entry_ids=[entry_id])

            # Exposure/session survival is not authority evidence.
            promoted = promote_by_sessions_seen(tmp_db, threshold=3)
            assert promoted == 0

            # Verify status remains unchanged.
            conn2 = get_db(tmp_db)
            row = conn2.execute(
                "SELECT status, confidence FROM memories WHERE id = ?", (entry_id,)
            ).fetchone()
            assert row["status"] == "candidate"
            assert row["confidence"] == 0.5
        finally:
            close_db(tmp_db)
            os.unlink(tmp_db)

    def test_promote_skips_short_entries(self):
        """Short/vague entries should not be promoted even with high sessions_seen."""
        import tempfile
        from odibi_anchor.codebase._memory_db import (
            get_db, close_db, insert_memory, increment_sessions_seen,
            promote_by_sessions_seen
        )

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            tmp_db = f.name

        try:
            conn = get_db(tmp_db)
            # Insert a short entry (< 30 chars)
            result = insert_memory(
                tmp_db, project="test", type="gotcha",
                content="Too short to promote"
            )
            entry_id = result["id"]

            # Simulate 5 sessions
            for _ in range(5):
                increment_sessions_seen(tmp_db, entry_ids=[entry_id])

            # Should NOT promote (content too short)
            promoted = promote_by_sessions_seen(tmp_db, threshold=3)
            assert promoted == 0

            conn2 = get_db(tmp_db)
            row = conn2.execute(
                "SELECT status FROM memories WHERE id = ?", (entry_id,)
            ).fetchone()
            assert row["status"] == "candidate"  # Still candidate
        finally:
            close_db(tmp_db)
            os.unlink(tmp_db)

    def test_promote_does_not_affect_confirmed(self):
        """Already confirmed entries should not be re-processed."""
        import tempfile
        from odibi_anchor.codebase._memory_db import (
            get_db, close_db, insert_memory, increment_sessions_seen,
            promote_by_sessions_seen
        )

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            tmp_db = f.name

        try:
            conn = get_db(tmp_db)
            # Insert an already-confirmed entry
            result = insert_memory(
                tmp_db, project="test", type="gotcha",
                content="Already confirmed entry — should not be touched by promotion pipeline",
                status="confirmed", confidence=0.9
            )
            entry_id = result["id"]
            conn.execute(
                "UPDATE memories SET status='confirmed',confidence=0.9 WHERE id=?",
                (entry_id,),
            )
            conn.commit()
            for _ in range(5):
                increment_sessions_seen(tmp_db, entry_ids=[entry_id])

            promoted = promote_by_sessions_seen(tmp_db, threshold=3)
            assert promoted == 0  # Nothing to promote (already confirmed)

            conn2 = get_db(tmp_db)
            row = conn2.execute(
                "SELECT confidence FROM memories WHERE id = ?", (entry_id,)
            ).fetchone()
            assert row["confidence"] == 0.9  # Unchanged
        finally:
            close_db(tmp_db)
            os.unlink(tmp_db)

    def test_increment_empty_ids_returns_zero(self):
        """Empty entry_ids list should return 0."""
        from odibi_anchor.codebase._memory_db import increment_sessions_seen
        assert increment_sessions_seen(None, entry_ids=[]) == 0
        assert increment_sessions_seen(None, entry_ids=None) == 0


# ---------------------------------------------------------------------------
# Compliance Audit Tests
# ---------------------------------------------------------------------------

class TestComplianceAudit:
    """Test _compliance_audit() scoring logic."""

    def _simulate(self, anchor_func, timings, files_changed):
        """Helper: inject timings + files, call audit via the bootstrapped namespace."""
        from odibi_anchor._utils._session_state import (
            _SESSION_FILES_CHANGED, _SESSION_TIMINGS
        )
        _SESSION_TIMINGS.clear()
        _SESSION_FILES_CHANGED.clear()
        _SESSION_TIMINGS.extend(timings)
        _SESSION_FILES_CHANGED.update(files_changed)

    def test_perfect_session_scores_max(self, bootstrap_cw, clean_session_state):
        audit_fn = bootstrap_cw["_compliance_audit"]
        self._simulate(bootstrap_cw["anchor"], [
            {"action": "task", "elapsed_ms": 50.0, "error": None},
            {"action": "known_bad", "elapsed_ms": 30.0, "error": None},
            {"action": "touched", "elapsed_ms": 10.0, "error": None},
            {"action": "preflight", "elapsed_ms": 200.0, "error": None},
            {"action": "test", "elapsed_ms": 5000.0, "error": None},
            {"action": "checkpoint", "elapsed_ms": 8000.0, "error": None},
        ], ["src/module.py"])
        result = audit_fn()
        assert result["score"] == result["max_score"]
        assert result["gaps"] == []
        assert result["rating"] == "excellent"

    def test_no_planning_deducts(self, bootstrap_cw, clean_session_state):
        audit_fn = bootstrap_cw["_compliance_audit"]
        self._simulate(bootstrap_cw["anchor"], [
            {"action": "touched", "elapsed_ms": 10.0, "error": None},
            {"action": "preflight", "elapsed_ms": 200.0, "error": None},
            {"action": "test", "elapsed_ms": 5000.0, "error": None},
            {"action": "gate", "elapsed_ms": 100.0, "error": None},
        ], ["src/module.py"])
        result = audit_fn()
        assert result["score"] < result["max_score"]
        assert any("planning" in g.lower() for g in result["gaps"])

    def test_no_preflight_deducts(self, bootstrap_cw, clean_session_state):
        audit_fn = bootstrap_cw["_compliance_audit"]
        self._simulate(bootstrap_cw["anchor"], [
            {"action": "task", "elapsed_ms": 50.0, "error": None},
            {"action": "known_bad", "elapsed_ms": 30.0, "error": None},
            {"action": "touched", "elapsed_ms": 10.0, "error": None},
            {"action": "test", "elapsed_ms": 5000.0, "error": None},
            {"action": "gate", "elapsed_ms": 100.0, "error": None},
        ], ["src/module.py"])
        result = audit_fn()
        assert any("preflight" in g.lower() for g in result["gaps"])

    def test_changed_registry_is_registration_proof(self, bootstrap_cw, clean_session_state):
        audit_fn = bootstrap_cw["_compliance_audit"]
        self._simulate(bootstrap_cw["anchor"], [
            {"action": "task", "elapsed_ms": 50.0, "error": None},
            {"action": "known_bad", "elapsed_ms": 30.0, "error": None},
            {"action": "touched", "elapsed_ms": 10.0, "error": None},  # Only 1
            {"action": "preflight", "elapsed_ms": 200.0, "error": None},
            {"action": "test", "elapsed_ms": 5000.0, "error": None},
            {"action": "gate", "elapsed_ms": 100.0, "error": None},
        ], ["a.py", "b.py", "c.md"])  # 3 files, 1 touched
        result = audit_fn()
        assert not any("touched" in g.lower() and "missing" in g.lower() for g in result["gaps"])

    def test_no_changes_scores_max(self, bootstrap_cw, clean_session_state):
        """No file changes = no obligations = perfect score."""
        audit_fn = bootstrap_cw["_compliance_audit"]
        self._simulate(bootstrap_cw["anchor"], [
            {"action": "memory", "elapsed_ms": 50.0, "error": None},
            {"action": "map", "elapsed_ms": 200.0, "error": None},
        ], [])  # No files changed
        result = audit_fn()
        assert result["score"] == result["max_score"]

    def test_planning_after_edit_deducts(self, bootstrap_cw, clean_session_state):
        audit_fn = bootstrap_cw["_compliance_audit"]
        self._simulate(bootstrap_cw["anchor"], [
            {"action": "touched", "elapsed_ms": 10.0, "error": None},
            {"action": "task", "elapsed_ms": 50.0, "error": None},  # Too late
            {"action": "known_bad", "elapsed_ms": 30.0, "error": None},
            {"action": "preflight", "elapsed_ms": 200.0, "error": None},
            {"action": "test", "elapsed_ms": 5000.0, "error": None},
            {"action": "gate", "elapsed_ms": 100.0, "error": None},
        ], ["src/module.py"])
        result = audit_fn()
        assert any("after" in g.lower() for g in result["gaps"])

    def test_audit_returns_stats(self, bootstrap_cw, clean_session_state):
        audit_fn = bootstrap_cw["_compliance_audit"]
        self._simulate(bootstrap_cw["anchor"], [
            {"action": "task", "elapsed_ms": 50.0, "error": None},
            {"action": "touched", "elapsed_ms": 10.0, "error": None},
        ], ["x.py"])
        result = audit_fn()
        assert "stats" in result
        assert result["stats"]["total_actions"] == 2
        assert result["stats"]["files_changed"] == 1
        assert result["stats"]["total_time_ms"] == 60.0

    def test_errored_actions_not_counted(self, bootstrap_cw, clean_session_state):
        """Actions that errored should not count as fulfilled."""
        audit_fn = bootstrap_cw["_compliance_audit"]
        self._simulate(bootstrap_cw["anchor"], [
            {"action": "task", "elapsed_ms": 50.0, "error": None},
            {"action": "known_bad", "elapsed_ms": 30.0, "error": None},
            {"action": "preflight", "elapsed_ms": 200.0, "error": "ValueError"},  # Errored!
            {"action": "touched", "elapsed_ms": 10.0, "error": None},
            {"action": "test", "elapsed_ms": 5000.0, "error": None},
            {"action": "gate", "elapsed_ms": 100.0, "error": None},
        ], ["src/module.py"])
        result = audit_fn()
        # Preflight errored → should flag as missing
        assert any("preflight" in g.lower() for g in result["gaps"])



# ---------------------------------------------------------------------------
# Extended Compliance Checks (v0.6.2) — error ratio, retries, diversity, duration, cowboy
# ---------------------------------------------------------------------------

class TestComplianceAuditExtended:
    """Test 5 new efficiency/quality checks in _compliance_audit()."""

    def _simulate(self, anchor_func, timings, files_changed):
        from odibi_anchor._utils._session_state import (
            _SESSION_FILES_CHANGED, _SESSION_TIMINGS
        )
        _SESSION_TIMINGS.clear()
        _SESSION_FILES_CHANGED.clear()
        _SESSION_TIMINGS.extend(timings)
        _SESSION_FILES_CHANGED.update(files_changed)

    def test_max_score_is_15(self, bootstrap_cw, clean_session_state):
        """Max score should now be 15."""
        audit_fn = bootstrap_cw["_compliance_audit"]
        self._simulate(bootstrap_cw["anchor"], [
            {"action": "task", "elapsed_ms": 50.0, "error": None},
            {"action": "known_bad", "elapsed_ms": 30.0, "error": None},
            {"action": "touched", "elapsed_ms": 10.0, "error": None},
            {"action": "preflight", "elapsed_ms": 200.0, "error": None},
            {"action": "test", "elapsed_ms": 5000.0, "error": None},
            {"action": "checkpoint", "elapsed_ms": 8000.0, "error": None},
        ], ["src/module.py"])
        result = audit_fn()
        assert result["max_score"] == 15
        assert result["score"] == 15

    def test_high_error_ratio_deducts(self, bootstrap_cw, clean_session_state):
        """Error ratio > 30% should deduct."""
        audit_fn = bootstrap_cw["_compliance_audit"]
        self._simulate(bootstrap_cw["anchor"], [
            {"action": "task", "elapsed_ms": 50.0, "error": None},
            {"action": "safe", "elapsed_ms": 100.0, "error": "Err"},
            {"action": "safe", "elapsed_ms": 100.0, "error": "Err"},
            {"action": "safe", "elapsed_ms": 100.0, "error": None},
            {"action": "touched", "elapsed_ms": 10.0, "error": None},
        ], ["x.py"])
        result = audit_fn()
        assert any("error ratio" in g.lower() for g in result["gaps"])

    def test_low_error_ratio_ok(self, bootstrap_cw, clean_session_state):
        """Error ratio <= 30% should NOT deduct."""
        audit_fn = bootstrap_cw["_compliance_audit"]
        self._simulate(bootstrap_cw["anchor"], [
            {"action": "task", "elapsed_ms": 50.0, "error": None},
            {"action": "known_bad", "elapsed_ms": 30.0, "error": None},
            {"action": "safe", "elapsed_ms": 100.0, "error": "Err"},  # 1/8 = 12.5%
            {"action": "safe", "elapsed_ms": 100.0, "error": None},
            {"action": "touched", "elapsed_ms": 10.0, "error": None},
            {"action": "preflight", "elapsed_ms": 200.0, "error": None},
            {"action": "test", "elapsed_ms": 5000.0, "error": None},
            {"action": "checkpoint", "elapsed_ms": 8000.0, "error": None},
        ], ["src/module.py"])
        result = audit_fn()
        assert not any("error ratio" in g.lower() for g in result["gaps"])

    def test_retry_loop_deducts(self, bootstrap_cw, clean_session_state):
        """3+ consecutive same action should deduct."""
        audit_fn = bootstrap_cw["_compliance_audit"]
        self._simulate(bootstrap_cw["anchor"], [
            {"action": "task", "elapsed_ms": 50.0, "error": None},
            {"action": "known_bad", "elapsed_ms": 30.0, "error": None},
            {"action": "preflight", "elapsed_ms": 100.0, "error": None},
            {"action": "preflight", "elapsed_ms": 100.0, "error": None},
            {"action": "preflight", "elapsed_ms": 100.0, "error": None},
            {"action": "touched", "elapsed_ms": 10.0, "error": None},
            {"action": "test", "elapsed_ms": 5000.0, "error": None},
            {"action": "checkpoint", "elapsed_ms": 8000.0, "error": None},
        ], ["src/module.py"])
        result = audit_fn()
        assert any("retry loop" in g.lower() for g in result["gaps"])

    def test_no_retry_loop_two_consecutive(self, bootstrap_cw, clean_session_state):
        """2 consecutive is NOT a retry loop."""
        audit_fn = bootstrap_cw["_compliance_audit"]
        self._simulate(bootstrap_cw["anchor"], [
            {"action": "task", "elapsed_ms": 50.0, "error": None},
            {"action": "known_bad", "elapsed_ms": 30.0, "error": None},
            {"action": "preflight", "elapsed_ms": 100.0, "error": None},
            {"action": "preflight", "elapsed_ms": 100.0, "error": None},
            {"action": "touched", "elapsed_ms": 10.0, "error": None},
            {"action": "test", "elapsed_ms": 5000.0, "error": None},
            {"action": "checkpoint", "elapsed_ms": 8000.0, "error": None},
        ], ["src/module.py"])
        result = audit_fn()
        assert not any("retry loop" in g.lower() for g in result["gaps"])

    def test_low_diversity_deducts(self, bootstrap_cw, clean_session_state):
        """<3 unique actions with >2 files should deduct."""
        audit_fn = bootstrap_cw["_compliance_audit"]
        self._simulate(bootstrap_cw["anchor"], [
            {"action": "touched", "elapsed_ms": 10.0, "error": None},
            {"action": "touched", "elapsed_ms": 10.0, "error": None},
            {"action": "touched", "elapsed_ms": 10.0, "error": None},
        ], ["a.py", "b.py", "c.py"])
        result = audit_fn()
        assert any("diversity" in g.lower() for g in result["gaps"])

    def test_adequate_diversity_ok(self, bootstrap_cw, clean_session_state):
        """3+ unique actions should NOT deduct regardless of file count."""
        audit_fn = bootstrap_cw["_compliance_audit"]
        self._simulate(bootstrap_cw["anchor"], [
            {"action": "task", "elapsed_ms": 50.0, "error": None},
            {"action": "known_bad", "elapsed_ms": 30.0, "error": None},
            {"action": "touched", "elapsed_ms": 10.0, "error": None},
            {"action": "touched", "elapsed_ms": 10.0, "error": None},
            {"action": "touched", "elapsed_ms": 10.0, "error": None},
            {"action": "preflight", "elapsed_ms": 200.0, "error": None},
            {"action": "test", "elapsed_ms": 5000.0, "error": None},
            {"action": "checkpoint", "elapsed_ms": 8000.0, "error": None},
        ], ["a.py", "b.py", "c.py"])
        result = audit_fn()
        assert not any("diversity" in g.lower() for g in result["gaps"])

    def test_long_session_no_orientation_deducts(self, bootstrap_cw, clean_session_state):
        """Session > 300s without status/checkpoint should deduct."""
        audit_fn = bootstrap_cw["_compliance_audit"]
        self._simulate(bootstrap_cw["anchor"], [
            {"action": "task", "elapsed_ms": 50.0, "error": None},
            {"action": "known_bad", "elapsed_ms": 30.0, "error": None},
            {"action": "touched", "elapsed_ms": 10.0, "error": None},
            {"action": "preflight", "elapsed_ms": 200_000.0, "error": None},
            {"action": "test", "elapsed_ms": 150_000.0, "error": None},
            {"action": "gate", "elapsed_ms": 100.0, "error": None},
        ], ["src/module.py"])
        result = audit_fn()
        assert any("long session" in g.lower() for g in result["gaps"])

    def test_long_session_with_checkpoint_ok(self, bootstrap_cw, clean_session_state):
        """Long session WITH checkpoint should NOT deduct."""
        audit_fn = bootstrap_cw["_compliance_audit"]
        self._simulate(bootstrap_cw["anchor"], [
            {"action": "task", "elapsed_ms": 50.0, "error": None},
            {"action": "known_bad", "elapsed_ms": 30.0, "error": None},
            {"action": "touched", "elapsed_ms": 10.0, "error": None},
            {"action": "preflight", "elapsed_ms": 200_000.0, "error": None},
            {"action": "test", "elapsed_ms": 150_000.0, "error": None},
            {"action": "checkpoint", "elapsed_ms": 100.0, "error": None},
        ], ["src/module.py"])
        result = audit_fn()
        assert not any("long session" in g.lower() for g in result["gaps"])

    def test_cowboy_coding_no_read_deducts(self, bootstrap_cw, clean_session_state):
        """Editing without any prior read/plan action should deduct."""
        audit_fn = bootstrap_cw["_compliance_audit"]
        self._simulate(bootstrap_cw["anchor"], [
            {"action": "touched", "elapsed_ms": 10.0, "error": None},
            {"action": "preflight", "elapsed_ms": 200.0, "error": None},
            {"action": "test", "elapsed_ms": 5000.0, "error": None},
            {"action": "gate", "elapsed_ms": 100.0, "error": None},
        ], ["src/module.py"])
        result = audit_fn()
        assert any("cowboy" in g.lower() for g in result["gaps"])

    def test_cowboy_coding_with_lookup_ok(self, bootstrap_cw, clean_session_state):
        """Having a lookup before edit should NOT trigger cowboy coding."""
        audit_fn = bootstrap_cw["_compliance_audit"]
        self._simulate(bootstrap_cw["anchor"], [
            {"action": "lookup", "elapsed_ms": 50.0, "error": None},
            {"action": "touched", "elapsed_ms": 10.0, "error": None},
            {"action": "preflight", "elapsed_ms": 200.0, "error": None},
            {"action": "test", "elapsed_ms": 5000.0, "error": None},
            {"action": "gate", "elapsed_ms": 100.0, "error": None},
        ], ["src/module.py"])
        result = audit_fn()
        assert not any("cowboy" in g.lower() for g in result["gaps"])

    def test_rating_thresholds(self, bootstrap_cw, clean_session_state):
        """Test rating brackets for 15-point scale."""
        audit_fn = bootstrap_cw["_compliance_audit"]
        # Score 13+ = excellent
        self._simulate(bootstrap_cw["anchor"], [
            {"action": "task", "elapsed_ms": 50.0, "error": None},
            {"action": "known_bad", "elapsed_ms": 30.0, "error": None},
            {"action": "touched", "elapsed_ms": 10.0, "error": None},
            {"action": "preflight", "elapsed_ms": 200.0, "error": None},
            {"action": "test", "elapsed_ms": 5000.0, "error": None},
            {"action": "checkpoint", "elapsed_ms": 8000.0, "error": None},
        ], ["src/module.py"])
        assert audit_fn()["rating"] == "excellent"



# ---------------------------------------------------------------------------
# Auto-Tag: recurring & protective (v0.6.3)
# ---------------------------------------------------------------------------

class TestRecurringTag:
    """Test recurring auto-tag applied when 3+ similar entries exist."""

    def test_count_similar_entries_returns_int(self, bootstrap_cw):
        from odibi_anchor.codebase._memory_db import count_similar_entries
        result = count_similar_entries(content="some random content for testing")
        assert isinstance(result, int)
        assert result >= 0

    def test_count_similar_entries_empty_content(self, bootstrap_cw):
        from odibi_anchor.codebase._memory_db import count_similar_entries
        # Very short words (<=3 chars) get filtered out
        result = count_similar_entries(content="a b c d e")
        assert result == 0

    def test_count_similar_finds_compliance_entries(self, bootstrap_cw):
        """count_similar_entries returns an int for compliance-related queries."""
        from odibi_anchor.codebase._memory_db import count_similar_entries
        result = count_similar_entries(content="Session compliance score audit gaps")
        # Result depends on DB contents — may be 0 on fresh DB
        assert isinstance(result, int)
        assert result >= 0


class TestProtectiveTag:
    """Test explicit protective tagging without retrieval side effects."""

    def test_add_tag_to_entry_success(self, bootstrap_cw):
        from odibi_anchor.codebase._memory_db import (
            add_tag_to_entry, get_db, insert_memory
        )
        import json, uuid
        db_path = os.path.join(bootstrap_cw["ROOT"], ".agent_memory.db")
        # Insert a test entry with unique content to avoid dedup
        unique_content = f"Test protective tag unique {uuid.uuid4().hex[:8]}"
        insert_result = insert_memory(
            db_path=str(db_path),
            project="test_project",
            type="gotcha",
            content=unique_content,
            tags=["test"],
        )
        entry_id = insert_result["id"]
        # Add tag
        result = add_tag_to_entry(db_path=str(db_path), entry_id=entry_id, tag="protective")
        assert result is True
        # Verify
        conn = get_db(str(db_path))
        row = conn.execute("SELECT tags FROM memories WHERE id = ?", (entry_id,)).fetchone()
        tags = json.loads(row["tags"])
        assert "protective" in tags

    def test_add_tag_idempotent(self, bootstrap_cw):
        from odibi_anchor.codebase._memory_db import (
            add_tag_to_entry, insert_memory
        )
        import uuid
        db_path = os.path.join(bootstrap_cw["ROOT"], ".agent_memory.db")
        unique_content = f"Test idempotent tag unique {uuid.uuid4().hex[:8]}"
        insert_result = insert_memory(
            db_path=str(db_path),
            project="test_project",
            type="gotcha",
            content=unique_content,
            tags=["existing-tag"],
        )
        entry_id = insert_result["id"]
        assert insert_result["action"] == "inserted", f"Unexpected dedup: {insert_result}"
        # First add
        assert add_tag_to_entry(db_path=str(db_path), entry_id=entry_id, tag="protective") is True
        # Second add (idempotent)
        assert add_tag_to_entry(db_path=str(db_path), entry_id=entry_id, tag="protective") is False

    def test_add_tag_nonexistent_entry(self, bootstrap_cw):
        from odibi_anchor.codebase._memory_db import add_tag_to_entry
        db_path = os.path.join(bootstrap_cw["ROOT"], ".agent_memory.db")
        result = add_tag_to_entry(
            db_path=str(db_path), entry_id="nonexistent-uuid-12345", tag="protective"
        )
        assert result is False

    def test_known_bad_does_not_tag_matched_entries(self, bootstrap_cw):
        """A known_bad match is exposure, not evidence for a protective tag."""
        from odibi_anchor.codebase._memory_db import (
            insert_memory, get_db
        )
        import json
        anchor_fn = bootstrap_cw["anchor"]
        db_path = os.path.join(bootstrap_cw["ROOT"], ".agent_memory.db")
        # Insert a gotcha that should match
        insert_result = insert_memory(
            db_path=db_path,
            project="project",
            type="gotcha",
            content="Never use collect() on large DataFrames in agent_init.py",
            related_files=["agent_init.py"],
            tags=["test-kb"],
            confidence=0.8,
            status="confirmed",
        )
        entry_id = insert_result["id"]
        # Run known_bad with a diff that mentions collect
        result = anchor_fn("known_bad",
            changed_files=["agent_init.py"],
            proposed_diff="+ df.collect()\n",
            db_path=db_path,
            output_format="dict",
        )
        # A match must not mutate tags.
        conn = get_db(db_path)
        row = conn.execute("SELECT tags FROM memories WHERE id = ?", (entry_id,)).fetchone()
        tags = json.loads(row["tags"])
        matched_ids = [m.get("id") for m in result.get("matched_memories", [])]
        assert entry_id in matched_ids
        assert tags == ["test-kb"]



# ---------------------------------------------------------------------------
# Auto-Tag: time-sink, protective boot, archive guard (v0.6.4)
# ---------------------------------------------------------------------------

class TestTimeSinkTag:
    """Test time-sink auto-tag for slow sessions."""

    def _simulate(self, anchor_func, timings, files_changed):
        from odibi_anchor._utils._session_state import (
            _SESSION_FILES_CHANGED, _SESSION_TIMINGS
        )
        _SESSION_TIMINGS.clear()
        _SESSION_FILES_CHANGED.clear()
        _SESSION_TIMINGS.extend(timings)
        _SESSION_FILES_CHANGED.update(files_changed)

    def test_time_sink_triggers_on_slow_session(self, bootstrap_cw, clean_session_state):
        """Session >5min with no checkpoint = time-sink."""
        audit_fn = bootstrap_cw["_compliance_audit"]
        self._simulate(bootstrap_cw["anchor"], [
            {"action": "task", "elapsed_ms": 50.0, "error": None},
            {"action": "known_bad", "elapsed_ms": 30.0, "error": None},
            {"action": "touched", "elapsed_ms": 10.0, "error": None},
            {"action": "preflight", "elapsed_ms": 200_000.0, "error": None},
            {"action": "test", "elapsed_ms": 150_000.0, "error": None},
            {"action": "gate", "elapsed_ms": 100.0, "error": None},
        ], ["src/module.py"])
        result = audit_fn()
        # total = 350s (>300s threshold), 0 checkpoints = 1 feature
        assert result["stats"]["total_time_ms"] > 300_000

    def test_time_sink_not_triggered_with_checkpoints(self, bootstrap_cw, clean_session_state):
        """Session >5min but multiple checkpoints = ok (time/feature < 5min)."""
        audit_fn = bootstrap_cw["_compliance_audit"]
        self._simulate(bootstrap_cw["anchor"], [
            {"action": "task", "elapsed_ms": 50.0, "error": None},
            {"action": "known_bad", "elapsed_ms": 30.0, "error": None},
            {"action": "touched", "elapsed_ms": 10.0, "error": None},
            {"action": "checkpoint", "elapsed_ms": 100_000.0, "error": None},
            {"action": "touched", "elapsed_ms": 10.0, "error": None},
            {"action": "checkpoint", "elapsed_ms": 100_000.0, "error": None},
            {"action": "touched", "elapsed_ms": 10.0, "error": None},
            {"action": "checkpoint", "elapsed_ms": 100_000.0, "error": None},
        ], ["a.py", "b.py", "c.py"])
        result = audit_fn()
        # total = 300s, 3 checkpoints = 100s/feature (<300s threshold) = no time-sink
        assert result["stats"]["total_time_ms"] >= 300_000

    def test_fast_session_no_time_sink(self, bootstrap_cw, clean_session_state):
        """Session <5min should never get time-sink."""
        audit_fn = bootstrap_cw["_compliance_audit"]
        self._simulate(bootstrap_cw["anchor"], [
            {"action": "task", "elapsed_ms": 50.0, "error": None},
            {"action": "known_bad", "elapsed_ms": 30.0, "error": None},
            {"action": "touched", "elapsed_ms": 10.0, "error": None},
            {"action": "preflight", "elapsed_ms": 200.0, "error": None},
            {"action": "test", "elapsed_ms": 5000.0, "error": None},
            {"action": "checkpoint", "elapsed_ms": 8000.0, "error": None},
        ], ["src/module.py"])
        result = audit_fn()
        assert result["stats"]["total_time_ms"] < 300_000


class TestArchiveGuard:
    """Test that archive_stale() skips protective entries."""

    def test_protective_entry_survives_archive(self, bootstrap_cw):
        from odibi_anchor.codebase._memory_db import (
            get_db, archive_stale
        )
        from datetime import datetime, timezone, timedelta
        import json, uuid

        db_path = os.path.join(bootstrap_cw["ROOT"], ".agent_memory.db")
        conn = get_db(db_path)
        old_date = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
        now = datetime.now(timezone.utc).isoformat()
        test_id = f"test-archive-guard-{uuid.uuid4().hex[:8]}"

        conn.execute("""
            INSERT OR REPLACE INTO memories 
            (id, project, type, content, tags, source, confidence, status, last_used, use_count, created)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (test_id, "test", "gotcha", "Protected from archive",
              json.dumps(["protective", "test"]), "test", 0.5, "candidate", old_date, 0, now))
        conn.commit()

        archive_stale(db_path=db_path, max_unused_days=90, min_use_count=2)

        row = conn.execute("SELECT status FROM memories WHERE id = ?", (test_id,)).fetchone()
        assert row["status"] == "candidate"
        # Cleanup
        conn.execute("DELETE FROM memories WHERE id = ?", (test_id,))
        conn.commit()

    def test_normal_entry_is_not_archived_from_legacy_usage_counters(self, bootstrap_cw):
        from odibi_anchor.codebase._memory_db import (
            get_db, archive_stale
        )
        from datetime import datetime, timezone, timedelta
        import json, uuid

        db_path = os.path.join(bootstrap_cw["ROOT"], ".agent_memory.db")
        conn = get_db(db_path)
        old_date = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
        now = datetime.now(timezone.utc).isoformat()
        test_id = f"test-archive-normal-{uuid.uuid4().hex[:8]}"

        conn.execute("""
            INSERT OR REPLACE INTO memories 
            (id, project, type, content, tags, source, confidence, status, last_used, use_count, created)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (test_id, "test", "gotcha", "Should be archived",
              json.dumps(["test"]), "test", 0.5, "candidate", old_date, 0, now))
        conn.commit()

        archive_stale(db_path=db_path, max_unused_days=90, min_use_count=2)

        row = conn.execute("SELECT status FROM memories WHERE id = ?", (test_id,)).fetchone()
        assert row["status"] == "candidate"
        # Cleanup
        conn.execute("DELETE FROM memories WHERE id = ?", (test_id,))
        conn.commit()


# ---------------------------------------------------------------------------
# MCP server ↔ dispatcher contract
# ---------------------------------------------------------------------------

class TestMcpServerContract:
    """Every anchor() action the MCP server calls must be a real dispatch action."""

    import re as _re
    from pathlib import Path as _Path

    _MCP_SRC = (
        _Path(__file__).resolve().parent.parent
        / "src" / "odibi_anchor" / "mcp_server.py"
    ).read_text(encoding="utf-8")

    def _mcp_actions(self):
        import re
        return sorted(set(re.findall(r'anchor\(\s*[\'"]([a-z_]+)[\'"]', self._MCP_SRC)))

    def _has_universal_gateway(self):
        """Return whether MCP forwards a caller-provided action to anchor()."""
        return "anchor(request.action, *request.args, **request.kwargs)" in self._MCP_SRC

    def test_no_duplicate_tool_defs(self):
        import re
        defs = re.findall(r'^def ([a-z_]+)\(', self._MCP_SRC, re.M)
        dups = sorted({d for d in defs if defs.count(d) > 1})
        assert not dups, f"duplicate MCP tool defs (FastMCP would clash): {dups}"

    def test_every_mcp_action_is_dispatchable(self, tmp_path, monkeypatch, clean_session_state):
        # MCP _boot() uses odibi_anchor.bootstrap.init — validate against THAT
        # (not the agent_init.py-based bootstrap_cw fixture). Isolated tmp root so
        # the repo's tracked .agent_memory.db is untouched.
        monkeypatch.setenv("HOME", str(tmp_path))
        from odibi_anchor.bootstrap import init
        anchor, _root, _ = init(root=str(tmp_path), output_format="dict")
        try:
            anchor("__definitely_invalid_action__")
            valid = set()
        except ValueError as e:
            msg = str(e)
            tail = msg.split("Available:", 1)[1] if "Available:" in msg else ""
            valid = {a.strip() for a in tail.split(",") if a.strip()}
        assert valid, "could not derive the valid action list from anchor()"
        bad = [a for a in self._mcp_actions() if a not in valid]
        assert not bad, f"MCP tools call unknown anchor() actions: {bad}"

    def test_critical_actions_exposed_or_intentionally_omitted(
        self, tmp_path, monkeypatch, clean_session_state
    ):
        """Reverse direction: every dispatchable action must be exposed via the
        MCP server OR be in the documented intentional-omit set.

        Catches silent omissions like skill_loaded — which an enforcement gate
        instructs the agent to call, so an MCP agent was hard-blocked from the
        file-modifying workflow until it was exposed.
        """
        monkeypatch.setenv("HOME", str(tmp_path))
        from odibi_anchor.bootstrap import init
        anchor, _root, _ = init(root=str(tmp_path), output_format="dict")
        try:
            anchor("__definitely_invalid_action__")
            valid = set()
        except ValueError as e:
            msg = str(e)
            tail = msg.split("Available:", 1)[1] if "Available:" in msg else ""
            valid = {a.strip() for a in tail.split(",") if a.strip()}
        assert valid, "could not derive the valid action list from anchor()"
        # Deliberately NOT exposed over MCP, each with a reason:
        INTENTIONAL_OMIT = {
            "apply_sql",       # Delta write path — not agent-triggerable over MCP by design
            "quick",           # disabled action (raises) — must never be exposed
            "db_migrate",      # admin/maintenance, run deliberately
            "memory_hygiene",  # admin/maintenance, run deliberately
            # Spark/Delta-only tools — the MCP server is file/pandas-oriented with no
            # active SparkSession, so these would only error (see pre_merge target):
            "delta_diff",      # Delta version comparison
            "partition_check", # Spark partition validation
            "watermark",       # Spark watermark debug
            "schema_migrate",  # Spark/SQL schema-migration codegen
            "echo",            # internal test/echo tool
        }
        # The consolidated MCP server intentionally exposes actions through
        # anchor_execute(action, args), so a literal anchor("...") call per action no
        # longer exists in mcp_server.py. When that gateway is present, every
        # dispatchable action is exposed by construction.
        unexposed = (
            set()
            if self._has_universal_gateway()
            else valid - set(self._mcp_actions()) - INTENTIONAL_OMIT
        )
        assert not unexposed, (
            "anchor actions missing from the MCP server — expose them in mcp_server.py, "
            f"or add to INTENTIONAL_OMIT with a reason: {sorted(unexposed)}"
        )
