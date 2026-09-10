"""Tests for cross-session regression detection (_session_health.py)."""

import json
import os

import pytest

from odibi_anchor._dispatcher._session_health import (
    _file_hash,
    _enforcement_hash,
    _load_health,
    capture_session_health,
    check_cross_session_drift,
    session_delta_context,
    _HEALTH_FILE,
)


class TestFileHash:
    def test_returns_sha256_for_valid_file(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text("hello\n")
        h = _file_hash(str(f))
        assert h is not None
        assert len(h) == 64  # SHA-256 hex length

    def test_returns_none_for_missing_file(self, tmp_path):
        assert _file_hash(str(tmp_path / "nonexistent.py")) is None

    def test_same_content_same_hash(self, tmp_path):
        f1 = tmp_path / "a.py"
        f2 = tmp_path / "b.py"
        f1.write_text("content")
        f2.write_text("content")
        assert _file_hash(str(f1)) == _file_hash(str(f2))

    def test_different_content_different_hash(self, tmp_path):
        f1 = tmp_path / "a.py"
        f2 = tmp_path / "b.py"
        f1.write_text("content_a")
        f2.write_text("content_b")
        assert _file_hash(str(f1)) != _file_hash(str(f2))


class TestCaptureSessionHealth:
    def test_writes_health_file(self, tmp_path):
        src = tmp_path / "src.py"
        src.write_text("print(1)\n")
        result = capture_session_health(
            str(tmp_path),
            {str(src)},
            [{"action": "test", "elapsed_ms": 50, "error": None}],
        )
        health_path = tmp_path / _HEALTH_FILE
        assert health_path.exists()
        data = json.loads(health_path.read_text())
        assert "session_id" in data
        assert "file_hashes" in data
        assert "threshold_values" in data

    def test_snapshot_contains_file_hashes(self, tmp_path):
        src = tmp_path / "module.py"
        src.write_text("x = 1\n")
        result = capture_session_health(
            str(tmp_path), {str(src)}, [],
        )
        assert str(src) in result["file_hashes"]
        assert len(result["file_hashes"][str(src)]) == 64

    def test_default_threshold_values(self, tmp_path):
        result = capture_session_health(str(tmp_path), set(), [])
        assert result["threshold_values"] == {
            "checkpoint_file_threshold": 20,
            "max_ungated_edits": 20,
        }

    def test_custom_threshold_values(self, tmp_path):
        custom = {"checkpoint_file_threshold": 10, "max_ungated_edits": 8}
        result = capture_session_health(
            str(tmp_path), set(), [], threshold_values=custom,
        )
        assert result["threshold_values"] == custom

    def test_optional_test_and_preflight_results(self, tmp_path):
        result = capture_session_health(
            str(tmp_path), set(), [],
            test_result={"passed": 5, "failed": 0},
            preflight_result={"is_safe": True},
        )
        assert result["test_result"] == {"passed": 5, "failed": 0}
        assert result["preflight_result"] == {"is_safe": True}

    def test_skips_unreadable_files(self, tmp_path):
        result = capture_session_health(
            str(tmp_path), {"/nonexistent/path.py"}, [],
        )
        assert result["file_hashes"] == {}

    def test_snapshot_under_10kb(self, tmp_path):
        """Health snapshot must stay lightweight (< 10KB)."""
        files = set()
        for i in range(20):
            f = tmp_path / f"file_{i}.py"
            f.write_text(f"# file {i}\n")
            files.add(str(f))
        capture_session_health(str(tmp_path), files, [])
        health_path = tmp_path / _HEALTH_FILE
        assert health_path.stat().st_size < 10240


class TestLoadHealth:
    def test_returns_none_when_no_file(self, tmp_path):
        assert _load_health(str(tmp_path)) is None

    def test_returns_dict_when_valid(self, tmp_path):
        health_path = tmp_path / _HEALTH_FILE
        health_path.write_text(json.dumps({"session_id": "test", "file_hashes": {}}))
        result = _load_health(str(tmp_path))
        assert result == {"session_id": "test", "file_hashes": {}}

    def test_returns_none_for_corrupt_json(self, tmp_path):
        health_path = tmp_path / _HEALTH_FILE
        health_path.write_text("{invalid json")
        assert _load_health(str(tmp_path)) is None


class TestCheckCrossSessionDrift:
    def test_no_health_file_returns_empty(self, tmp_path):
        assert check_cross_session_drift(str(tmp_path)) == []

    def test_no_drift_when_files_unchanged(self, tmp_path):
        src = tmp_path / "code.py"
        src.write_text("original\n")
        # Capture exit snapshot
        capture_session_health(str(tmp_path), {str(src)}, [])
        # Check — file unchanged
        warnings = check_cross_session_drift(str(tmp_path))
        assert warnings == []

    def test_detects_file_modification(self, tmp_path):
        src = tmp_path / "code.py"
        src.write_text("original\n")
        capture_session_health(str(tmp_path), {str(src)}, [])
        # Simulate external modification
        src.write_text("modified\n")
        warnings = check_cross_session_drift(str(tmp_path))
        assert len(warnings) == 1
        assert "DRIFT" in warnings[0]
        assert "modified outside session" in warnings[0]

    def test_detects_file_deletion(self, tmp_path):
        src = tmp_path / "code.py"
        src.write_text("original\n")
        capture_session_health(str(tmp_path), {str(src)}, [])
        # Delete file
        src.unlink()
        warnings = check_cross_session_drift(str(tmp_path))
        assert len(warnings) == 1
        assert "deleted outside session" in warnings[0]

    def test_detects_threshold_drift(self, tmp_path):
        # Capture with non-default threshold
        capture_session_health(
            str(tmp_path), set(), [],
            threshold_values={"checkpoint_file_threshold": 10, "max_ungated_edits": 6},
        )
        # Current code has threshold=5, so drift detected
        warnings = check_cross_session_drift(str(tmp_path))
        assert any("CONFIG" in w for w in warnings)
        assert any("checkpoint_file_threshold" in w for w in warnings)


class TestSessionDeltaContext:
    def test_no_health_file_returns_clean_context(self, tmp_path):
        result = session_delta_context(str(tmp_path), output_format="dict")
        assert result["metrics"]["has_previous_snapshot"] is False
        assert "No .anchor_session_health.json" in result["findings"][0]

    def test_no_drift_reports_clean(self, tmp_path):
        src = tmp_path / "file.py"
        src.write_text("code\n")
        capture_session_health(str(tmp_path), {str(src)}, [])
        result = session_delta_context(str(tmp_path), output_format="dict")
        assert result["metrics"]["has_drift"] is False
        assert result["metrics"]["files_modified_outside_session"] == 0

    def test_drift_reported_in_metrics(self, tmp_path):
        src = tmp_path / "file.py"
        src.write_text("code\n")
        capture_session_health(str(tmp_path), {str(src)}, [])
        src.write_text("changed\n")
        result = session_delta_context(str(tmp_path), output_format="dict")
        assert result["metrics"]["has_drift"] is True
        assert result["metrics"]["files_modified_outside_session"] == 1
        assert str(src) in result["files_modified_outside_session"]

    def test_markdown_output(self, tmp_path):
        result = session_delta_context(str(tmp_path), output_format="markdown")
        assert isinstance(result, str)
        assert "# Session Delta" in result

    def test_deleted_file_in_delta(self, tmp_path):
        src = tmp_path / "will_delete.py"
        src.write_text("temp\n")
        capture_session_health(str(tmp_path), {str(src)}, [])
        src.unlink()
        result = session_delta_context(str(tmp_path), output_format="dict")
        assert result["metrics"]["files_deleted_outside_session"] == 1
        assert str(src) in result["files_deleted_outside_session"]


class TestEnforcementHash:
    def test_enforcement_hash_returns_string(self):
        """_enforcement_hash should return a SHA-256 hex string."""
        h = _enforcement_hash()
        assert h is not None
        assert len(h) == 64

    def test_snapshot_includes_enforcement_hash(self, tmp_path):
        result = capture_session_health(str(tmp_path), set(), [])
        assert "enforcement_functions_hash" in result
        assert result["enforcement_functions_hash"] is not None
        assert len(result["enforcement_functions_hash"]) == 64

    def test_enforcement_drift_not_triggered_when_unchanged(self, tmp_path):
        """Same enforcement file → no ENFORCEMENT warning."""
        capture_session_health(str(tmp_path), set(), [])
        warnings = check_cross_session_drift(str(tmp_path))
        assert not any("ENFORCEMENT" in w for w in warnings)

    def test_enforcement_drift_triggered_when_hash_differs(self, tmp_path):
        """Simulated enforcement change → ENFORCEMENT warning."""
        # Capture with a fake enforcement hash
        health_path = tmp_path / _HEALTH_FILE
        snapshot = {
            "session_id": "test",
            "file_hashes": {},
            "threshold_values": {"checkpoint_file_threshold": 5, "max_ungated_edits": 6},
            "enforcement_functions_hash": "0" * 64,  # Fake hash that won't match
        }
        import json
        health_path.write_text(json.dumps(snapshot))
        warnings = check_cross_session_drift(str(tmp_path))
        assert any("ENFORCEMENT" in w for w in warnings)

    def test_old_health_file_without_enforcement_hash_no_warning(self, tmp_path):
        """Old snapshot missing enforcement_functions_hash → no crash, no warning."""
        health_path = tmp_path / _HEALTH_FILE
        snapshot = {
            "session_id": "old",
            "file_hashes": {},
            "threshold_values": {"checkpoint_file_threshold": 5, "max_ungated_edits": 6},
            # No enforcement_functions_hash key
        }
        import json
        health_path.write_text(json.dumps(snapshot))
        warnings = check_cross_session_drift(str(tmp_path))
        assert not any("ENFORCEMENT" in w for w in warnings)

    def test_delta_reports_enforcement_changed(self, tmp_path):
        """session_delta_context reports enforcement_changed in metrics."""
        health_path = tmp_path / _HEALTH_FILE
        snapshot = {
            "session_id": "test",
            "file_hashes": {},
            "threshold_values": {"checkpoint_file_threshold": 5, "max_ungated_edits": 6},
            "enforcement_functions_hash": "0" * 64,
        }
        import json
        health_path.write_text(json.dumps(snapshot))
        result = session_delta_context(str(tmp_path), output_format="dict")
        assert result["metrics"]["enforcement_changed"] is True
