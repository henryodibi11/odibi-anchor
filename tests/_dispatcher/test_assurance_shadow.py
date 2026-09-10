"""Transactional and fail-open integration tests for assurance shadow mode."""

from __future__ import annotations

from copy import deepcopy

import pytest


def _stage(state, **overrides):
    from odibi_anchor._dispatcher._task_injection import inject_session_context

    kwargs = {
        "mode": "analysis",
        "goal": "Produce deterministic advisory assurance output",
        "known_facts": ["Existing decisions remain authoritative"],
        "acceptance_criteria": ["The shadow projection is additive"],
        **overrides,
    }
    return inject_session_context(
        kwargs,
        None,
        set(),
        False,
        task_text="Assess the accepted task without changing authority",
        session_state=state,
    )


def _project(injected):
    from odibi_anchor.planning.task_execution_context import task_execution_context

    stage = injected.pop("_task_policy_stage")
    projected = task_execution_context(
        "Assess the accepted task without changing authority",
        **injected,
    )
    assert isinstance(projected, dict)
    return stage, projected


def _post_task(state, stage, projected, *, readiness=100, timings=None):
    from odibi_anchor._dispatcher._post_dispatch import run_post_dispatch

    result = deepcopy(projected)
    result["readiness"] = {"score": readiness, "missing_details": ["goal"]}
    return run_post_dispatch(
        "task",
        result,
        None,
        ("Assess the accepted task without changing authority",),
        {"mode": "analysis"},
        session_timings=(
            [{"action": "task", "passed": True, "error": None}]
            if timings is None else timings
        ),
        session_files_changed=set(),
        session_state=state,
        planning_required_actions=frozenset(),
        task_stage=stage,
    )


def _gate_state(*, execution_mode="read_only", epoch=0):
    from odibi_anchor._utils._session_state import SessionState
    from odibi_anchor.assurance import build_assurance_plan
    from odibi_anchor.planning._task_profile import normalize_task_profile

    profile = normalize_task_profile(execution_mode=execution_mode)
    state = SessionState(active_task_profile=profile, task_verification_epoch=epoch)
    state.active_assurance_plan = build_assurance_plan(profile)
    return state


def _legacy_gate_result():
    return {
        "passed": True,
        "overall_pass": True,
        "status": "pass",
        "exit_status": 0,
        "summary": "legacy gate passed",
        "metrics": {
            "risk_level": "low",
            "timing_verification": {"verified": True},
            "obligation_accounting": {"paid": 2, "required": 2},
        },
        "findings": ["legacy finding"],
        "required_actions": ["legacy action"],
        "obligations": ["legacy obligation"],
        "gate_exceptions": ["legacy exception"],
    }


def test_task_readiness_boundary_stages_then_commits_exact_plan_and_assessment():
    state = _gate_state(execution_mode="read_only")
    previous_plan = state.active_assurance_plan
    stage, projected = _project(_stage(state, risk="medium"))

    assert projected["assurance"] == {"plan": stage["assurance_plan"].to_dict()}
    assert state.active_assurance_plan is previous_plan

    with pytest.raises(RuntimeError, match="readiness too low"):
        _post_task(state, stage, projected, readiness=39)
    assert state.active_assurance_plan is previous_plan

    accepted = _post_task(state, stage, projected, readiness=40)
    assert accepted["assurance"]["plan"] == stage["assurance_plan"].to_dict()
    assert accepted["assurance"]["assessment"]["mode"] == "shadow"
    assert accepted["assurance"]["assessment"]["plan"] == accepted["assurance"]["plan"]
    functional = accepted["assurance"]["assessment"]["results"][0]
    assert functional["control_id"] == "AK-001"
    assert functional["evidence_state"] == "satisfied"
    assert state.active_assurance_plan == stage["assurance_plan"]


def test_planner_rejects_plan_that_does_not_match_exact_profile():
    from odibi_anchor._utils._session_state import SessionState
    from odibi_anchor.assurance import build_assurance_plan
    from odibi_anchor.planning._task_profile import normalize_task_profile
    from odibi_anchor.planning.task_execution_context import task_execution_context

    injected = _stage(SessionState())
    injected.pop("_task_policy_stage")
    injected["_assurance_plan"] = build_assurance_plan(
        normalize_task_profile(execution_mode="artifact_only")
    )

    with pytest.raises(ValueError, match="does not match"):
        task_execution_context("Mismatch", **injected)


def test_task_assessment_failure_is_visible_open_and_commits_staged_plan(monkeypatch):
    import odibi_anchor.assurance as assurance
    from odibi_anchor._utils._session_state import SessionState

    state = SessionState()
    stage, projected = _project(_stage(state))
    monkeypatch.setattr(
        assurance,
        "evaluate_assurance",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("shadow unavailable")),
    )

    accepted = _post_task(state, stage, projected)

    assert accepted["assurance"]["assessment"]["status"] == "degraded"
    assert accepted["task_profile"] == stage["profile"].to_dict()
    assert state.active_assurance_plan == stage["assurance_plan"]


@pytest.mark.parametrize(
    ("timings", "epoch", "evidence_state"),
    [
        (
            [
                {"action": "task", "passed": True, "error": None},
                {"action": "test", "passed": True, "error": None, "exit_code": 0},
            ],
            1,
            "satisfied",
        ),
        (
            [
                {"action": "task", "passed": True, "error": None},
                {"action": "test", "passed": False, "error": "AssertionError"},
            ],
            1,
            "failed",
        ),
        (
            [
                {"action": "test", "passed": True, "error": None},
                {"action": "task", "passed": True, "error": None},
            ],
            2,
            "stale",
        ),
        (
            [
                {"action": "task", "passed": True, "error": None},
                {"action": "test", "passed": True, "error": None, "unavailable": True},
            ],
            1,
            "unavailable",
        ),
        ([{"action": "task", "passed": True, "error": None}], 1, "missing"),
    ],
)
def test_gate_projection_preserves_every_legacy_decision_field(
    timings, epoch, evidence_state,
):
    from odibi_anchor._dispatcher._gate_wrappers import _attach_assurance_shadow

    state = _gate_state(epoch=epoch)
    legacy = _legacy_gate_result()
    before = deepcopy(legacy)

    projected = _attach_assurance_shadow(
        legacy,
        session_state=state,
        session_timings=timings,
        deferred=False,
    )

    for key in (
        "passed", "overall_pass", "status", "exit_status", "summary",
        "required_actions", "obligations", "gate_exceptions",
    ):
        assert projected[key] == before[key]
    for key in ("risk_level", "timing_verification", "obligation_accounting"):
        assert projected["metrics"][key] == before["metrics"][key]
    assert projected["findings"][: len(before["findings"])] == before["findings"]
    shadow = projected["metrics"]["assurance_shadow"]
    result = next(item for item in shadow["results"] if item["control_id"] == "AK-001")
    assert result["evidence_state"] == evidence_state
    assert shadow["mode"] == "shadow"


def test_gate_refresh_uses_new_evidence_and_deferred_projection_never_commits_state():
    from odibi_anchor._dispatcher._gate_wrappers import _attach_assurance_shadow

    state = _gate_state(epoch=1)
    accepted_plan = state.active_assurance_plan
    missing = _attach_assurance_shadow(
        _legacy_gate_result(),
        session_state=state,
        session_timings=[],
        deferred=False,
    )["metrics"]["assurance_shadow"]
    refreshed = _attach_assurance_shadow(
        _legacy_gate_result(),
        session_state=state,
        session_timings=[
            {"action": "task", "passed": True, "error": None},
            {"action": "test", "passed": True, "error": None},
        ],
        deferred=True,
    )["metrics"]["assurance_shadow"]

    assert missing != refreshed
    assert state.active_assurance_plan is accepted_plan
    assert not hasattr(state, "active_assurance_assessment")


def test_successful_test_result_provenance_can_satisfy_maintainability():
    from odibi_anchor._dispatcher._gate_wrappers import _attach_assurance_shadow
    from odibi_anchor._dispatcher._post_dispatch import run_post_dispatch

    state = _gate_state(execution_mode="source_change", epoch=1)
    timings = [
        {"action": "task", "passed": True, "error": None},
        {"action": "test", "passed": True, "error": None, "test_target": "tests/"},
    ]
    run_post_dispatch(
        "test",
        {
            "kind": "test_run",
            "metrics": {"exit_code": 0},
        },
        None,
        (),
        {},
        session_timings=timings,
        session_files_changed=set(),
        session_state=state,
        planning_required_actions=frozenset(),
    )

    shadow = _attach_assurance_shadow(
        _legacy_gate_result(),
        session_state=state,
        session_timings=timings,
        deferred=False,
    )["metrics"]["assurance_shadow"]
    maintainability = next(
        item for item in shadow["results"] if item["control_id"] == "AK-003"
    )

    assert timings[-1]["result"] == "test_run"
    assert timings[-1]["exit_code"] == 0
    assert maintainability["evidence_state"] == "satisfied"


def test_gate_assurance_failure_is_visible_and_preserves_plan_and_legacy_result(monkeypatch):
    import odibi_anchor.assurance as assurance
    from odibi_anchor._dispatcher._gate_wrappers import _attach_assurance_shadow

    state = _gate_state()
    accepted_plan = state.active_assurance_plan
    legacy = _legacy_gate_result()
    monkeypatch.setattr(
        assurance,
        "evaluate_assurance",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("shadow unavailable")),
    )

    projected = _attach_assurance_shadow(
        legacy,
        session_state=state,
        session_timings=[],
        deferred=False,
    )

    assert projected["passed"] is True
    assert projected["overall_pass"] is True
    assert projected["required_actions"] == ["legacy action"]
    assert projected["metrics"]["assurance_shadow"]["status"] == "degraded"
    assert projected["findings"][-1].startswith("SHADOW:")
    assert state.active_assurance_plan is accepted_plan


def test_missing_assurance_state_preserves_exact_pre_slice_gate_result():
    from odibi_anchor._dispatcher._gate_wrappers import _attach_assurance_shadow
    from odibi_anchor._utils._session_state import SessionState
    from odibi_anchor.planning._task_profile import normalize_task_profile

    state = SessionState(active_task_profile=normalize_task_profile())
    legacy = _legacy_gate_result()
    before = deepcopy(legacy)

    assert _attach_assurance_shadow(
        legacy, session_state=state, session_timings=[], deferred=False,
    ) == before


@pytest.mark.parametrize("continuation", [False, True])
def test_replacement_task_reset_clears_old_evidence_and_keeps_only_new_plan(continuation):
    from odibi_anchor.planning._task_policy import EvidenceEntry

    state = _gate_state()
    state.evidence_ledger = [
        EvidenceEntry(
            "old", "test", "pass", "old-task", "2026-08-20T10:00:00+00:00", {},
        )
    ]
    stage, projected = _project(
        _stage(state, execution_mode="artifact_only", continuation=continuation)
    )

    _post_task(state, stage, projected)

    assert state.evidence_ledger == []
    assert state.active_assurance_plan == stage["assurance_plan"]
    assert state.active_assurance_plan is not None
    assert state.active_assurance_plan.tier == "T1"


def test_task_reset_clears_assurance_plan():
    from odibi_anchor._utils._session_state import reset_task_policy_state

    state = _gate_state()

    reset_task_policy_state(state)

    assert state.active_assurance_plan is None
