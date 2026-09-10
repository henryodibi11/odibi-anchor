"""D-010: hard gate — a data-mode unit of work cannot close without output evidence.

Closes the "validates clean, never run, never profiled" hole: a build executed
outside anchor's write surfaces (e.g. an odibi/Databricks pipeline run) could be
marked done with zero data verification. The gate now requires a verification
action *after* the task before it will close for data modes.
"""
from dataclasses import replace
from types import SimpleNamespace

from odibi_anchor._dispatcher._enforcement import (
    should_block_gate_outcome_evidence,
)

DATA_MODES = frozenset({"data", "etl", "reconciliation"})


def _t(action, error=None, passed=False):
    return {"action": action, "error": error, "passed": passed, "elapsed_ms": 1.0}


# ── Non-data modes are never gated ──

def test_non_data_mode_never_blocks():
    assert not should_block_gate_outcome_evidence(
        "implementation", [_t("task")], DATA_MODES
    )[0]


def test_none_mode_never_blocks():
    assert not should_block_gate_outcome_evidence(None, [_t("task")], DATA_MODES)[0]


# ── The core failure: data mode, task planned, nothing verified ──

def test_block_data_mode_no_verification():
    # The Pattern-1 failure: planned, "built" (outside anchor), profiled nothing.
    timings = [_t("status"), _t("task"), _t("review")]
    blocked, msg = should_block_gate_outcome_evidence("data", timings, DATA_MODES)
    assert blocked
    assert "cannot close without verifying an output" in msg


def test_block_empty_timings_data_mode():
    assert should_block_gate_outcome_evidence("data", [], DATA_MODES)[0]


# ── Verification AFTER the task satisfies the gate ──

def test_pass_profile_table_after_task():
    timings = [_t("task"), _t("profile_table")]
    assert not should_block_gate_outcome_evidence("data", timings, DATA_MODES)[0]


def test_pass_quality_after_task():
    timings = [_t("task"), _t("quality")]
    assert not should_block_gate_outcome_evidence("etl", timings, DATA_MODES)[0]


def test_pass_contract_after_task():
    timings = [_t("task"), _t("contract")]
    assert not should_block_gate_outcome_evidence("reconciliation", timings, DATA_MODES)[0]


def test_pass_investigate_after_task():
    timings = [_t("task"), _t("investigate")]
    assert not should_block_gate_outcome_evidence("data", timings, DATA_MODES)[0]


# ── Planning-phase source profiling (BEFORE task) does not count ──

def test_source_profile_before_task_does_not_count():
    # profile_table ran during planning (before task); nothing verified after.
    timings = [_t("profile_table"), _t("task"), _t("review")]
    assert should_block_gate_outcome_evidence("data", timings, DATA_MODES)[0]


# ── Failed verification does not count ──

def test_failed_verification_does_not_count():
    timings = [_t("task"), _t("profile_table", error="boom")]
    assert should_block_gate_outcome_evidence("data", timings, DATA_MODES)[0]


# ── Window resets at a passing gate/checkpoint ──

def test_verification_before_passing_gate_does_not_carry():
    # profiled + gated a prior feature; new task after, no fresh verification.
    timings = [
        _t("task"), _t("profile_table"), _t("gate", passed=True),
        _t("task"), _t("review"),
    ]
    assert should_block_gate_outcome_evidence("data", timings, DATA_MODES)[0]


def test_verification_after_new_task_post_gate_satisfies():
    timings = [
        _t("task"), _t("profile_table"), _t("gate", passed=True),
        _t("task"), _t("quality"),
    ]
    assert not should_block_gate_outcome_evidence("data", timings, DATA_MODES)[0]


def test_no_task_in_window_falls_back_to_since_gate():
    # No task after the passing gate; verification since gate still counts.
    timings = [_t("gate", passed=True), _t("profile_table")]
    assert not should_block_gate_outcome_evidence("data", timings, DATA_MODES)[0]


def test_caller_pass_claim_cannot_pay_promoted_result_backed_control():
    from odibi_anchor._dispatcher._gate_wrappers import _attach_assurance_shadow
    from odibi_anchor._utils._session_state import SessionState
    from odibi_anchor.assurance import T1_CONTROLS, build_assurance_plan
    from odibi_anchor.planning._task_policy import EvidenceEntry
    from odibi_anchor.planning._task_profile import normalize_task_profile

    profile = normalize_task_profile(execution_mode="source_change")
    state = SessionState(active_task_profile=profile, task_verification_epoch=1)
    state.active_assurance_plan = build_assurance_plan(profile)
    state.assurance_t1_controls = tuple(  # type: ignore[attr-defined]
        replace(control, disposition="blocking") for control in T1_CONTROLS
    )
    state.intended_pr_paths = ("src/mod.py",)
    state.evidence_ledger = [EvidenceEntry(
        "caller-tests", "tests", "pass", "caller",
        "2026-08-20T11:00:00+00:00", {"exit_code": 0},
    )]
    scope = SimpleNamespace(
        changed_paths=("src/mod.py",), changed_line_ranges=(),
        provenance={"target_current_sha": "a" * 40},
    )
    result = _attach_assurance_shadow(
        {"passed": True, "overall_pass": True, "status": "pass", "exit_status": 0,
         "summary": "legacy pass", "metrics": {}, "findings": [], "obligations": []},
        session_state=state,
        session_timings=[{"action": "task", "passed": True, "error": None}],
        deferred=False,
        task_scope=scope,
    )

    assert result["overall_pass"] is False
    assert result["status"] == "fail"
    tests = next(
        item for item in result["metrics"]["t1_assurance"]["results"]
        if item["control_id"] == "Anchor-T1-TESTS"
    )
    assert tests["evidence_state"] == "missing"
    assert "caller-tests" not in tests["evidence_ids"]
