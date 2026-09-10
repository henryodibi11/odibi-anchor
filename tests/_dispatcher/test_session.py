"""Tests for odibi_anchor._dispatcher._session — session state and config management."""

import json
import os
from types import SimpleNamespace

import pytest

import odibi_anchor._dispatcher._session as session_module
from odibi_anchor._dispatcher._project import RouteBinding
from odibi_anchor._dispatcher._session import (
    _SESSION_STATE_FILE,
    ContinuityUnavailable,
    _config,
    _load_continuity_state,
    _load_session_state,
    _save_continuity_state,
    _save_session_state,
)
from odibi_anchor.planning._task_builders import required_skills_for_task
from odibi_anchor.planning._task_profile import normalize_task_profile


class TestLoadSessionState:
    """Tests for _load_session_state."""

    def test_no_file__returns_empty_dict(self, tmp_path):
        result = _load_session_state(str(tmp_path))
        assert result == {}

    def test_valid_file__loads_content(self, tmp_path):
        state = {"key": "value", "count": 42}
        state_path = tmp_path / _SESSION_STATE_FILE
        state_path.write_text(json.dumps(state))
        result = _load_session_state(str(tmp_path))
        assert result == state

    def test_corrupt_json__returns_empty_dict(self, tmp_path):
        state_path = tmp_path / _SESSION_STATE_FILE
        state_path.write_text("not valid json {{")
        result = _load_session_state(str(tmp_path))
        assert result == {}

    def test_empty_file__returns_empty_dict(self, tmp_path):
        state_path = tmp_path / _SESSION_STATE_FILE
        state_path.write_text("")
        result = _load_session_state(str(tmp_path))
        assert result == {}

    def test_historical_removed_skill_names_are_readable_unchanged_but_inert(self, tmp_path):
        historical = {
            "session_id": "old-session",
            "skills_loaded": ["planning-delivery", "writing-specs"],
            "events": [{"action": "skill_loaded", "name": "code-quality"}],
        }
        state_path = tmp_path / _SESSION_STATE_FILE
        original = json.dumps(historical, indent=2) + "\n"
        state_path.write_text(original)

        assert _load_session_state(str(tmp_path)) == historical
        assert state_path.read_text() == original

        next_task = normalize_task_profile(legacy_mode="planning")
        assert required_skills_for_task(next_task) == ()


def _binding(root, *, project="project-a", runtime="runtime-a"):
    return RouteBinding(
        project_id=project,
        target_root=str(root / project / "target"),
        artifact_root=str(root / project / "artifacts"),
        anchor_home=str(root / "anchor-home"),
        binding_source="explicit",
        runtime_instance_id=runtime,
    )


def _continuity_state(*, session="session-a", task=None):
    return SimpleNamespace(
        session_id=session,
        task_window_id=task,
        continuity_generation=0,
        continuity_record_sha256=None,
        continuity_status="uninitialized",
    )


class TestRouteOwnedContinuity:
    def test_distinct_projects_and_runtimes_write_independent_records(self, tmp_path):
        a = _binding(tmp_path, project="project-a", runtime="runtime-a")
        b = _binding(tmp_path, project="project-b", runtime="runtime-b")
        state_a = _continuity_state(task="ltw-a")
        state_b = _continuity_state(session="session-b", task="ltw-b")

        saved_a = _save_continuity_state(a, state_a, {"value": "a"})
        saved_b = _save_continuity_state(b, state_b, {"value": "b"})

        assert saved_a["record_path"] != saved_b["record_path"]
        assert _load_continuity_state(a, state_a)["state"] == {"value": "a"}
        assert _load_continuity_state(b, state_b)["state"] == {"value": "b"}

    def test_same_artifact_root_with_foreign_owner_fails_closed(self, tmp_path):
        first = _binding(tmp_path, project="project-a")
        second = RouteBinding(
            project_id="project-b", target_root=str(tmp_path / "project-b" / "target"),
            artifact_root=first.artifact_root, anchor_home=first.anchor_home,
            binding_source="explicit", runtime_instance_id="runtime-b",
        )
        _save_continuity_state(first, _continuity_state(), {"owner": "a"})

        with pytest.raises(ContinuityUnavailable, match="owner mismatch"):
            _save_continuity_state(second, _continuity_state(session="session-b"), {"owner": "b"})

    def test_stale_writer_cannot_replace_newer_generation(self, tmp_path):
        binding = _binding(tmp_path)
        current = _continuity_state()
        stale = _continuity_state()
        first = _save_continuity_state(binding, current, {"generation": 1})
        _save_continuity_state(binding, current, {"generation": 2})

        stale.continuity_generation = first["generation"]
        stale.continuity_record_sha256 = first["record_sha256"]
        with pytest.raises(ContinuityUnavailable, match="stale continuity writer"):
            _save_continuity_state(binding, stale, {"generation": "stale"})

        assert _load_continuity_state(binding, current)["state"] == {"generation": 2}

    def test_interrupted_replace_leaves_verified_prior_generation(self, tmp_path, monkeypatch):
        binding = _binding(tmp_path)
        state = _continuity_state()
        _save_continuity_state(binding, state, {"generation": 1})

        monkeypatch.setattr(
            session_module._os, "replace",
            lambda *_args: (_ for _ in ()).throw(OSError("injected replace interruption")),
        )
        with pytest.raises(OSError, match="injected replace"):
            _save_continuity_state(binding, state, {"generation": 2})

        assert _load_continuity_state(binding, state)["state"] == {"generation": 1}

    def test_legacy_state_is_backed_up_but_not_used_as_task_authority(self, tmp_path):
        binding = _binding(tmp_path)
        os.makedirs(binding.artifact_root)
        legacy = {"task_window_id": "ltw-legacy", "awaiting_learn": True}
        legacy_path = os.path.join(binding.artifact_root, _SESSION_STATE_FILE)
        with open(legacy_path, "w", encoding="utf-8") as stream:
            json.dump(legacy, stream)

        result = _load_continuity_state(binding, _continuity_state())

        assert result["state"] == {}
        assert result["legacy_migration"]["status"] == "preserved_ambiguous"
        assert os.path.isfile(result["legacy_migration"]["backup_path"])


class TestSaveSessionState:
    """Tests for _save_session_state."""

    def test_creates_file(self, tmp_path):
        state = {"session": "test", "files": [1, 2, 3]}
        _save_session_state(str(tmp_path), state)
        state_path = tmp_path / _SESSION_STATE_FILE
        assert state_path.exists()
        loaded = json.loads(state_path.read_text())
        assert loaded == state

    def test_overwrites_existing(self, tmp_path):
        state_path = tmp_path / _SESSION_STATE_FILE
        state_path.write_text(json.dumps({"old": True}))
        _save_session_state(str(tmp_path), {"new": True})
        loaded = json.loads(state_path.read_text())
        assert loaded == {"new": True}

    def test_never_raises_on_bad_path(self):
        """Should silently handle write failures."""
        # /nonexistent path — should not raise
        _save_session_state("/nonexistent/path/xyz", {"data": 1})

    def test_windows_skips_only_directory_fsync(self, tmp_path, monkeypatch):
        directory = str(tmp_path)
        original_open = os.open
        original_replace = os.replace
        file_fsyncs = []
        replacements = []

        monkeypatch.setattr(session_module, "_CAN_FSYNC_DIRECTORY", False)

        def open_except_directory(path, flags, *args, **kwargs):
            assert os.fspath(path) != directory
            return original_open(path, flags, *args, **kwargs)

        def record_replace(source, destination):
            replacements.append((source, destination))
            return original_replace(source, destination)

        monkeypatch.setattr(os, "open", open_except_directory)
        monkeypatch.setattr(os, "fsync", lambda fd: file_fsyncs.append(fd))
        monkeypatch.setattr(os, "replace", record_replace)

        _save_session_state(directory, {"platform": "windows"}, strict=True)

        assert len(file_fsyncs) == 1
        assert len(replacements) == 1
        assert json.loads((tmp_path / _SESSION_STATE_FILE).read_text()) == {"platform": "windows"}

    def test_posix_fsyncs_and_closes_containing_directory(self, tmp_path, monkeypatch):
        directory = str(tmp_path)
        directory_fd = 98765
        original_open = os.open
        fsyncs = []
        closes = []

        monkeypatch.setattr(session_module, "_CAN_FSYNC_DIRECTORY", True)

        def open_directory(path, flags, *args, **kwargs):
            if os.fspath(path) == directory:
                assert flags == os.O_RDONLY
                return directory_fd
            return original_open(path, flags, *args, **kwargs)

        monkeypatch.setattr(os, "open", open_directory)
        monkeypatch.setattr(os, "fsync", lambda fd: fsyncs.append(fd))
        monkeypatch.setattr(os, "close", lambda fd: closes.append(fd))

        _save_session_state(directory, {"platform": "posix"}, strict=True)

        assert fsyncs[-1] == directory_fd
        assert len(fsyncs) == 2
        assert closes == [directory_fd]

    def test_windows_file_fsync_failure_remains_fatal_in_strict_mode(self, tmp_path, monkeypatch):
        replace_called = False

        monkeypatch.setattr(session_module, "_CAN_FSYNC_DIRECTORY", False)

        def fail_file_fsync(_fd):
            raise OSError("file fsync failed")

        def record_replace(_source, _destination):
            nonlocal replace_called
            replace_called = True

        monkeypatch.setattr(os, "fsync", fail_file_fsync)
        monkeypatch.setattr(os, "replace", record_replace)

        with pytest.raises(RuntimeError, match="session state could not be persisted atomically") as exc_info:
            _save_session_state(str(tmp_path), {"data": 1}, strict=True)

        assert isinstance(exc_info.value.__cause__, OSError)
        assert str(exc_info.value.__cause__) == "file fsync failed"
        assert replace_called is False

    def test_windows_atomic_replace_failure_remains_fatal(self, tmp_path, monkeypatch):
        fsyncs = []

        monkeypatch.setattr(session_module, "_CAN_FSYNC_DIRECTORY", False)
        monkeypatch.setattr(os, "fsync", lambda fd: fsyncs.append(fd))

        def fail_replace(_source, _destination):
            raise OSError("replace failed")

        monkeypatch.setattr(os, "replace", fail_replace)

        with pytest.raises(RuntimeError, match="session state could not be persisted atomically") as exc_info:
            _save_session_state(str(tmp_path), {"data": 1}, strict=True)

        assert len(fsyncs) == 1
        assert isinstance(exc_info.value.__cause__, OSError)
        assert str(exc_info.value.__cause__) == "replace failed"

    @pytest.mark.parametrize("failure_stage", ["open", "fsync"])
    def test_posix_directory_failure_remains_fatal(self, tmp_path, monkeypatch, failure_stage):
        directory = str(tmp_path)
        directory_fd = 98765
        original_open = os.open
        original_fsync = os.fsync
        original_close = os.close

        monkeypatch.setattr(session_module, "_CAN_FSYNC_DIRECTORY", True)

        def fail_directory_open(path, flags, *args, **kwargs):
            if os.fspath(path) == directory:
                if failure_stage == "open":
                    raise OSError("directory open failed")
                return directory_fd
            return original_open(path, flags, *args, **kwargs)

        def fail_directory_fsync(fd):
            if fd == directory_fd:
                raise OSError("directory fsync failed")
            return original_fsync(fd)

        monkeypatch.setattr(os, "open", fail_directory_open)
        monkeypatch.setattr(os, "fsync", fail_directory_fsync)
        monkeypatch.setattr(os, "close", lambda fd: None if fd == directory_fd else original_close(fd))

        with pytest.raises(RuntimeError, match="session state could not be persisted atomically") as exc_info:
            _save_session_state(directory, {"data": 1}, strict=True)

        assert isinstance(exc_info.value.__cause__, OSError)
        assert str(exc_info.value.__cause__) == f"directory {failure_stage} failed"


class TestConfig:
    """Tests for _config action."""

    def test_view_config__no_file(self, tmp_path):
        result = _config(str(tmp_path))
        assert result["kind"] == "config_context"
        assert result["metrics"]["active_categories"] == 11
        assert result["metrics"]["suppressed_categories"] == 0
        assert result["metrics"]["mutated"] is False

    def test_suppress_category__persists(self, tmp_path):
        result = _config(str(tmp_path), suppress_category="performance")
        assert result["metrics"]["mutated"] is True
        assert result["metrics"]["suppressed_categories"] == 1
        # Verify persisted
        config_path = tmp_path / ".anchor_config.json"
        assert config_path.exists()
        data = json.loads(config_path.read_text())
        assert "performance" in data["anti_patterns"]["suppress_categories"]

    def test_unsuppress_category(self, tmp_path):
        # First suppress
        _config(str(tmp_path), suppress_category="performance")
        # Then unsuppress
        result = _config(str(tmp_path), unsuppress_category="performance")
        assert result["metrics"]["suppressed_categories"] == 0
        assert result["metrics"]["mutated"] is True

    def test_suppress_id(self, tmp_path):
        result = _config(str(tmp_path), suppress_id="todo_fixme_hack")
        assert result["metrics"]["suppressed_ids"] == 1
        assert result["metrics"]["mutated"] is True

    def test_unsuppress_id(self, tmp_path):
        _config(str(tmp_path), suppress_id="todo_fixme_hack")
        result = _config(str(tmp_path), unsuppress_id="todo_fixme_hack")
        assert result["metrics"]["suppressed_ids"] == 0

    def test_file_override__add(self, tmp_path):
        result = _config(str(tmp_path), file_override="tests/**", suppress_categories=["performance"])
        assert result["metrics"]["file_overrides"] == 1

    def test_file_override__remove(self, tmp_path):
        _config(str(tmp_path), file_override="tests/**", suppress_categories=["performance"])
        result = _config(str(tmp_path), remove_override="tests/**")
        assert result["metrics"]["file_overrides"] == 0

    def test_idempotent_suppress(self, tmp_path):
        """Suppressing same category twice doesn't duplicate."""
        _config(str(tmp_path), suppress_category="performance")
        result = _config(str(tmp_path), suppress_category="performance")
        assert result["metrics"]["suppressed_categories"] == 1
        # Second call should not mutate
        assert result["metrics"]["mutated"] is False

    def test_markdown_output(self, tmp_path):
        result = _config(str(tmp_path), output_format="markdown")
        assert isinstance(result, str)
        assert "Anti-Pattern Config" in result

    def test_all_categories_list(self, tmp_path):
        result = _config(str(tmp_path))
        assert len(result["all_categories"]) == 11
        assert "performance" in result["all_categories"]
        assert "convention" in result["all_categories"]
        assert "security" in result["all_categories"]
