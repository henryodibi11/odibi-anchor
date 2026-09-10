"""Tests for odibi_anchor._utils._session_state — shared session state singleton.

Tests cover:
    - touched(): registration, ValueError on empty, syntax check, created flag
    - record_timing(): appends timing dict with correct shape
    - get_state(): returns snapshot with correct keys and values
    - is_empty(): transitions from True to False and back
    - Singleton coherence: same objects across re-imports
"""

import importlib
import json
import os
import sys

import pytest


# ─── Fixture provided by conftest.py ─────────────────────────────────────────
# clean_session_state (autouse) isolates each test from session state pollution.
# See tests/utils/conftest.py for implementation.


# ─── touched() ───────────────────────────────────────────────────────────────

class TestTouched:
    """Tests for the touched() function."""

    @pytest.mark.fast
    def test_registers_file_in_changed_set(self):
        from odibi_anchor._utils._session_state import (
            touched, _SESSION_FILES_CHANGED,
        )
        result = touched("src/my_module.py")
        assert "src/my_module.py" in _SESSION_FILES_CHANGED
        assert result["registered"] == "src/my_module.py"
        assert result["session_changed"] == 1

    @pytest.mark.fast
    def test_created_flag_registers_in_both_sets(self):
        from odibi_anchor._utils._session_state import (
            touched, _SESSION_FILES_CHANGED, _SESSION_FILES_CREATED,
        )
        result = touched("new_file.py", created=True)
        assert "new_file.py" in _SESSION_FILES_CHANGED
        assert "new_file.py" in _SESSION_FILES_CREATED
        assert result["created"] is True
        assert result["session_created"] == 1

    @pytest.mark.fast
    def test_not_created_flag_only_changed(self):
        from odibi_anchor._utils._session_state import (
            touched, _SESSION_FILES_CREATED,
        )
        touched("existing.py", created=False)
        assert "existing.py" not in _SESSION_FILES_CREATED

    @pytest.mark.fast
    def test_empty_path_raises_valueerror(self):
        from odibi_anchor._utils._session_state import touched
        with pytest.raises(ValueError, match="non-empty"):
            touched("")

    @pytest.mark.fast
    def test_whitespace_path_raises_valueerror(self):
        from odibi_anchor._utils._session_state import touched
        with pytest.raises(ValueError, match="non-empty"):
            touched("   ")

    @pytest.mark.fast
    def test_syntax_check_passed_on_valid_py(self, tmp_path):
        from odibi_anchor._utils._session_state import touched
        valid_file = tmp_path / "valid.py"
        valid_file.write_text("x = 1\n")
        result = touched(str(valid_file))
        assert result["syntax_check"] == "passed"
        assert "syntax_error" not in result

    @pytest.mark.fast
    def test_syntax_check_failed_on_invalid_py(self, tmp_path):
        from odibi_anchor._utils._session_state import touched
        bad_file = tmp_path / "bad.py"
        bad_file.write_text("def broken(\n")
        result = touched(str(bad_file))
        assert result["syntax_check"] == "FAILED"
        assert "syntax_error" in result
        assert "Line" in result["syntax_error"]

    @pytest.mark.fast
    def test_syntax_check_na_for_non_py(self):
        from odibi_anchor._utils._session_state import touched
        result = touched("data.json")
        assert result["syntax_check"] == "n/a"

    @pytest.mark.fast
    def test_syntax_check_skipped_for_missing_py(self):
        from odibi_anchor._utils._session_state import touched
        result = touched("/nonexistent/path/missing.py")
        # File doesn't exist → syntax_check not set (None → excluded)
        assert "syntax_check" not in result

    @pytest.mark.fast
    def test_relative_path_resolved_with_root(self, tmp_path):
        from odibi_anchor._utils._session_state import touched
        py_file = tmp_path / "module.py"
        py_file.write_text("y = 2\n")
        result = touched("module.py", root=str(tmp_path))
        assert result["syntax_check"] == "passed"

    @pytest.mark.fast
    def test_multiple_touches_accumulate(self):
        from odibi_anchor._utils._session_state import (
            touched, _SESSION_FILES_CHANGED,
        )
        touched("a.py")
        touched("b.py")
        touched("c.py")
        assert len(_SESSION_FILES_CHANGED) == 3
        r = touched("d.py")
        assert r["session_changed"] == 4

    @pytest.mark.fast
    def test_duplicate_touch_idempotent(self):
        from odibi_anchor._utils._session_state import (
            touched, _SESSION_FILES_CHANGED,
        )
        touched("same.py")
        touched("same.py")
        assert len(_SESSION_FILES_CHANGED) == 1


# ─── record_timing() ─────────────────────────────────────────────────────────

class TestRecordTiming:
    """Tests for the record_timing() function."""

    @pytest.mark.fast
    def test_appends_timing_entry(self):
        from odibi_anchor._utils._session_state import (
            record_timing, _SESSION_TIMINGS,
        )
        record_timing("map", 42.567)
        assert len(_SESSION_TIMINGS) == 1
        assert _SESSION_TIMINGS[0] == {
            "action": "map",
            "elapsed_ms": 42.6,
            "error": None,
        }

    @pytest.mark.fast
    def test_rounds_elapsed_ms(self):
        from odibi_anchor._utils._session_state import (
            record_timing, _SESSION_TIMINGS,
        )
        record_timing("test", 1.23456789)
        assert _SESSION_TIMINGS[0]["elapsed_ms"] == 1.2

    @pytest.mark.fast
    def test_records_error(self):
        from odibi_anchor._utils._session_state import (
            record_timing, _SESSION_TIMINGS,
        )
        record_timing("gate", 100.0, error="ValueError")
        assert _SESSION_TIMINGS[0]["error"] == "ValueError"

    @pytest.mark.fast
    def test_multiple_timings_accumulate(self):
        from odibi_anchor._utils._session_state import (
            record_timing, _SESSION_TIMINGS,
        )
        record_timing("a", 10.0)
        record_timing("b", 20.0)
        record_timing("c", 30.0)
        assert len(_SESSION_TIMINGS) == 3
        assert [t["action"] for t in _SESSION_TIMINGS] == ["a", "b", "c"]


# ─── get_state() ─────────────────────────────────────────────────────────────

class TestGetState:
    """Tests for the get_state() function."""

    @pytest.mark.fast
    def test_empty_state(self):
        from odibi_anchor._utils._session_state import get_state
        state = get_state()
        assert state["files_changed"] == []
        assert state["files_created"] == []
        assert state["timings"] == []
        assert state["total_changed"] == 0
        assert state["total_created"] == 0
        assert state["total_actions"] == 0
        assert state["total_time_ms"] == 0.0

    @pytest.mark.fast
    def test_reflects_touched_files(self):
        from odibi_anchor._utils._session_state import (
            touched, get_state,
        )
        touched("z.py")
        touched("a.py", created=True)
        state = get_state()
        assert state["files_changed"] == ["a.py", "z.py"]  # sorted
        assert state["files_created"] == ["a.py"]
        assert state["total_changed"] == 2
        assert state["total_created"] == 1

    @pytest.mark.fast
    def test_reflects_timings(self):
        from odibi_anchor._utils._session_state import (
            record_timing, get_state,
        )
        record_timing("x", 10.0)
        record_timing("y", 20.5)
        state = get_state()
        assert state["total_actions"] == 2
        assert state["total_time_ms"] == 30.5

    @pytest.mark.fast
    def test_required_keys_present(self):
        from odibi_anchor._utils._session_state import get_state
        state = get_state()
        required = {
            "files_changed", "files_created", "timings",
            "total_changed", "total_created", "total_actions", "total_time_ms",
            "active_assurance_plan",
        }
        assert required.issubset(state.keys())

    @pytest.mark.fast
    def test_assurance_plan_snapshot_is_json_compatible_and_excludes_assessment(
        self, monkeypatch,
    ):
        from odibi_anchor._utils._session_state import _SESSION_STATE, get_state
        from odibi_anchor.assurance import build_assurance_plan
        from odibi_anchor.planning._task_profile import normalize_task_profile

        plan = build_assurance_plan(normalize_task_profile(execution_mode="source_change"))
        monkeypatch.setattr(_SESSION_STATE, "active_assurance_plan", plan)

        snapshot = json.loads(json.dumps(get_state()))

        assert snapshot["active_assurance_plan"] == plan.to_dict()
        assert "assurance_assessment" not in snapshot


class TestAssurancePersistenceCompatibility:
    """Optional v1 assurance state is fail-open for legacy and malformed payloads."""

    @pytest.mark.fast
    def test_missing_plan_preserves_legacy_session_state(self):
        from odibi_anchor._utils._session_state import SessionState

        assert SessionState().active_assurance_plan is None

    @pytest.mark.fast
    def test_valid_serialized_plan_restores_strict_contract(self):
        from odibi_anchor._utils._session_state import SessionState
        from odibi_anchor.assurance import build_assurance_plan
        from odibi_anchor.planning._task_profile import normalize_task_profile

        plan = build_assurance_plan(normalize_task_profile(execution_mode="artifact_only"))
        restored = SessionState(
            active_assurance_plan=json.loads(json.dumps(plan.to_dict())),
        )

        assert restored.active_assurance_plan == plan

    @pytest.mark.fast
    @pytest.mark.parametrize("payload", [{"schema_version": "1.0"}, "malformed", 7])
    def test_malformed_persisted_plan_degrades_to_none(self, payload):
        from odibi_anchor._utils._session_state import SessionState

        assert SessionState(active_assurance_plan=payload).active_assurance_plan is None


# ─── is_empty() ──────────────────────────────────────────────────────────────

class TestIsEmpty:
    """Tests for the is_empty() function."""

    @pytest.mark.fast
    def test_true_when_clean(self):
        from odibi_anchor._utils._session_state import is_empty
        assert is_empty() is True

    @pytest.mark.fast
    def test_false_after_touch(self):
        from odibi_anchor._utils._session_state import (
            touched, is_empty,
        )
        touched("x.py")
        assert is_empty() is False

    @pytest.mark.fast
    def test_false_after_timing(self):
        from odibi_anchor._utils._session_state import (
            record_timing, is_empty,
        )
        record_timing("test", 1.0)
        assert is_empty() is False


# ─── Singleton coherence ─────────────────────────────────────────────────────

class TestSingletonCoherence:
    """Verify that re-importing returns the same objects."""

    @pytest.mark.fast
    def test_same_set_across_imports(self):
        from odibi_anchor._utils._session_state import _SESSION_FILES_CHANGED as s1
        from odibi_anchor._utils._session_state import _SESSION_FILES_CHANGED as s2
        assert s1 is s2

    @pytest.mark.fast
    def test_mutation_visible_across_imports(self):
        from odibi_anchor._utils._session_state import (
            _SESSION_FILES_CHANGED, touched,
        )
        touched("coherence_test.py")
        # Re-import and check
        mod = importlib.import_module("odibi_anchor._utils._session_state")
        assert "coherence_test.py" in mod._SESSION_FILES_CHANGED
        assert mod._SESSION_FILES_CHANGED is _SESSION_FILES_CHANGED
