"""H-003: gate runs known_bad guardrail on unregistered .py drift.

Blocks ONLY on a confirmed high-confidence match; clean drift is auto-touched
and surfaced as a finding (editAsset-safe).
"""
import importlib

import pytest

from odibi_anchor._dispatcher import _gate_wrappers as gw


@pytest.fixture
def kb_mod():
    """Resolve the live known_bad submodule via sys.modules — exactly what the
    gate's runtime `from ... import known_bad_change_context` reads. Resolving
    fresh (not a collection-time `import ... as`) avoids a stale reference if
    another test reloaded the module, and avoids the function/submodule name
    collision created by codebase/__init__ re-exporting the function."""
    return importlib.import_module(
        "odibi_anchor.codebase.known_bad_change_context"
    )


@pytest.fixture(autouse=True)
def _isolate_session_state():
    """Save/restore the shared session-state singletons so gate auto-touch
    (which writes to the global changed-files set) does not leak across tests."""
    import odibi_anchor._utils._session_state as session_state_module
    from odibi_anchor._utils._session_state import (
        _SESSION_FILES_CHANGED, _SESSION_FILES_CREATED,
        _SESSION_TIMINGS, _SESSION_DIFF_BASELINES,
        _SESSION_PROVEN_BASELINES, _SESSION_PROVEN_CREATED,
    )
    saved = (
        _SESSION_FILES_CHANGED.copy(), _SESSION_FILES_CREATED.copy(),
        list(_SESSION_TIMINGS), dict(_SESSION_DIFF_BASELINES),
        dict(session_state_module._SESSION_BOOT_MANIFEST),
        session_state_module._BOOT_MANIFEST_BUILT,
        session_state_module._BOOT_MANIFEST_ROOT,
        _SESSION_PROVEN_CREATED.copy(), dict(_SESSION_PROVEN_BASELINES),
    )
    _SESSION_FILES_CHANGED.clear()
    _SESSION_FILES_CREATED.clear()
    _SESSION_TIMINGS.clear()
    _SESSION_DIFF_BASELINES.clear()
    _SESSION_PROVEN_CREATED.clear()
    _SESSION_PROVEN_BASELINES.clear()
    session_state_module._SESSION_BOOT_MANIFEST = {}
    session_state_module._BOOT_MANIFEST_BUILT = False
    session_state_module._BOOT_MANIFEST_ROOT = None
    yield
    _SESSION_FILES_CHANGED.clear(); _SESSION_FILES_CHANGED.update(saved[0])
    _SESSION_FILES_CREATED.clear(); _SESSION_FILES_CREATED.update(saved[1])
    _SESSION_TIMINGS.clear(); _SESSION_TIMINGS.extend(saved[2])
    _SESSION_DIFF_BASELINES.clear(); _SESSION_DIFF_BASELINES.update(saved[3])
    session_state_module._SESSION_BOOT_MANIFEST = saved[4]
    session_state_module._BOOT_MANIFEST_BUILT = saved[5]
    session_state_module._BOOT_MANIFEST_ROOT = saved[6]
    _SESSION_PROVEN_CREATED.clear(); _SESSION_PROVEN_CREATED.update(saved[7])
    _SESSION_PROVEN_BASELINES.clear(); _SESSION_PROVEN_BASELINES.update(saved[8])


class _State:
    files_at_last_checkpoint = 0
    notebook_path = None
    active_spec = None
    active_task_mode = "implementation"
    skills_loaded = set()
    skill_hints_emitted = set()


def _passing_timings():
    # review, test, preflight satisfied so only the drift logic is exercised
    return [
        {"action": "task", "elapsed_ms": 1, "error": None, "passed": True,
         "task_mode": "implementation"},
        {"action": "review", "elapsed_ms": 1, "error": None},
        {"action": "preflight", "elapsed_ms": 1, "error": None},
        {"action": "test", "elapsed_ms": 1, "error": None, "passed": True},
    ]


def _run(
    tmp_path, timings, drift, *, changed_files=None, mode="implementation",
    reconcile=None, check_coverage=None,
):
    state = _State()
    state.active_task_mode = mode
    return gw.gate_with_auto_confirm(
        tmp_path, (), {},
        session_timings=timings,
        session_files_changed=changed_files if changed_files is not None else {"x.py"},
        session_files_created=set(),
        session_frame=None, frame_enabled=False, session_state=state,
        workflow_gate_fn=lambda *a, **k: {"metrics": {}, "findings": []},
        check_drift_fn=lambda root: drift,
        check_test_coverage_fn=check_coverage or (lambda **k: True),
        reconcile_ledger_fn=reconcile,
    )


def test_clean_py_drift_is_auto_touched_not_blocked(tmp_path, monkeypatch, kb_mod):
    drift = {"has_drift": True, "unregistered": ["x.py"], "created": []}
    monkeypatch.setattr(kb_mod, "known_bad_change_context",
                        lambda *a, **k: {"metrics": {"status": "ok"}, "risks": []})
    result = _run(tmp_path, _passing_timings(), drift)
    assert result["metrics"]["auto_touched_count"] == 1
    assert any("known_bad guardrail" in f for f in result["findings"])


def test_known_bad_match_in_drift_blocks(tmp_path, monkeypatch, kb_mod):
    drift = {"has_drift": True, "unregistered": ["x.py"], "created": []}
    monkeypatch.setattr(
        kb_mod, "known_bad_change_context",
        lambda *a, **k: {"metrics": {"status": "block"},
                         "risks": ["Known failure [x]: confirmed bad change"]},
    )
    with pytest.raises(RuntimeError, match="high-confidence"):
        _run(tmp_path, _passing_timings(), drift)


def test_warn_status_does_not_block(tmp_path, monkeypatch, kb_mod):
    drift = {"has_drift": True, "unregistered": ["x.py"], "created": []}
    monkeypatch.setattr(kb_mod, "known_bad_change_context",
                        lambda *a, **k: {"metrics": {"status": "warn"}, "risks": []})
    result = _run(tmp_path, _passing_timings(), drift)
    assert result["metrics"]["auto_touched_count"] == 1


def test_known_bad_already_run_skips_recheck(tmp_path, monkeypatch, kb_mod):
    drift = {"has_drift": True, "unregistered": ["x.py"], "created": []}
    called = {"n": 0}

    def _spy(*a, **k):
        called["n"] += 1
        return {"metrics": {"status": "block"}, "risks": []}

    monkeypatch.setattr(kb_mod, "known_bad_change_context", _spy)
    timings = _passing_timings() + [{"action": "known_bad", "elapsed_ms": 1, "error": None}]
    _run(tmp_path, timings, drift)
    assert called["n"] == 0  # known_bad ran this session — no gate-time re-check


def test_gate_validates_resolved_auto_scoped_target_collection(tmp_path):
    timings = _passing_timings()
    timings[-1]["test_target"] = ["tests/test_other.py"]

    with pytest.raises(RuntimeError, match="doesn't cover"):
        _run(
            tmp_path,
            timings,
            {"has_drift": False, "unregistered": [], "created": []},
            changed_files={"src/module.py"},
            check_coverage=lambda **kwargs: kwargs["test_target"] != ["tests/test_other.py"],
        )


def test_gate_rejects_marker_filtered_run_as_general_coverage(tmp_path):
    timings = _passing_timings()
    timings[-1].update({"test_target": "tests/", "test_mark": "fast"})

    with pytest.raises(RuntimeError, match="marker filter"):
        _run(
            tmp_path,
            timings,
            {"has_drift": False, "unregistered": [], "created": []},
            changed_files={"src/module.py"},
        )


def test_gate_rejects_targetless_marker_run_as_general_coverage(tmp_path):
    timings = _passing_timings()
    timings[-1]["test_mark"] = "fast"

    with pytest.raises(RuntimeError, match="marker filter"):
        _run(
            tmp_path,
            timings,
            {"has_drift": False, "unregistered": [], "created": []},
            changed_files={"src/module.py"},
        )


def test_documentation_gate_blocks_non_markdown_drift_before_registration(tmp_path):
    drift = {"has_drift": True, "unregistered": ["settings.json"], "created": []}
    with pytest.raises(RuntimeError, match="Documentation mode"):
        _run(
            tmp_path, _passing_timings(), drift,
            changed_files={"README.md"}, mode="documentation",
        )


def test_documentation_gate_allows_and_registers_markdown_drift(tmp_path):
    drift = {"has_drift": True, "unregistered": ["guide.markdown"], "created": []}
    result = _run(
        tmp_path, _passing_timings(), drift,
        changed_files={"README.md"}, mode="documentation",
    )
    assert result["metrics"]["auto_touched_count"] == 1


def test_documentation_gate_rechecks_existing_final_ledger(tmp_path):
    drift = {"has_drift": False, "unregistered": [], "created": []}
    with pytest.raises(RuntimeError, match="Documentation mode"):
        _run(
            tmp_path, _passing_timings(), drift,
            changed_files={"README.md", "settings.json"}, mode="documentation",
        )


def test_documentation_gate_retry_discards_deleted_created_file(tmp_path):
    from odibi_anchor._utils._session_state import (
        _SESSION_FILES_CHANGED,
        build_boot_manifest,
        install_write_guard,
        reconcile_session_file_ledger,
    )

    build_boot_manifest(str(tmp_path))
    install_write_guard(str(tmp_path))
    rejected = tmp_path / "settings.json"
    rejected.write_text("{}", encoding="utf-8")
    rejected.unlink()

    result = _run(
        tmp_path,
        _passing_timings(),
        {"has_drift": False, "unregistered": [], "created": []},
        changed_files=_SESSION_FILES_CHANGED,
        mode="documentation",
        reconcile=reconcile_session_file_ledger,
    )

    assert "settings.json" not in _SESSION_FILES_CHANGED
    assert result["metrics"].get("auto_touched_count", 0) == 0
    assert result["metrics"]["reconciled_file_count"] == 1
