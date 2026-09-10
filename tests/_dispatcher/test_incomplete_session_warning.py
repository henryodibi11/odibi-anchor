"""H-009: anchor('status') warns when a prior session changed files without a passing gate.

_compliance.py imports the session-state globals into its OWN namespace, so the
patch targets the _compliance module (patching _session_state would not affect
the names _status actually reads).
"""
import json

import odibi_anchor._dispatcher._compliance as comp


def _one_status_timing():
    return [{"action": "status", "elapsed_ms": 1.0, "error": None}]


def _patch_clean_session(monkeypatch):
    monkeypatch.setattr(comp, "_SESSION_TIMINGS", _one_status_timing())
    monkeypatch.setattr(comp, "_SESSION_FILES_CHANGED", set())
    monkeypatch.setattr(comp, "_SESSION_FILES_CREATED", set())


def test_warning_when_prior_session_changed_files_without_gate(tmp_path, monkeypatch):
    (tmp_path / ".anchor_session_health.json").write_text(
        json.dumps({"files_changed": {"a.py": "deadbeef"}, "gate_passed": False})
    )
    _patch_clean_session(monkeypatch)
    ctx = comp._status(str(tmp_path), {}, None, {}, output_format="dict")
    assert any("Previous session changed" in a for a in ctx["suggested_next_actions"])


def test_no_warning_when_prior_gate_passed(tmp_path, monkeypatch):
    (tmp_path / ".anchor_session_health.json").write_text(
        json.dumps({"files_changed": {"a.py": "x"}, "gate_passed": True})
    )
    _patch_clean_session(monkeypatch)
    ctx = comp._status(str(tmp_path), {}, None, {}, output_format="dict")
    assert not any("Previous session changed" in a for a in ctx["suggested_next_actions"])


def test_no_warning_when_no_health_file(tmp_path, monkeypatch):
    _patch_clean_session(monkeypatch)
    ctx = comp._status(str(tmp_path), {}, None, {}, output_format="dict")
    assert not any("Previous session changed" in a for a in ctx["suggested_next_actions"])
