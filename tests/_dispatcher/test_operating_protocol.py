"""Focused qualification of the additive operating-protocol projection."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from odibi_anchor._dispatcher._agent_context import attach_agent_context
from odibi_anchor._dispatcher._effects import enforce_effects, task_profile_effect_compatible
from odibi_anchor._dispatcher._operating_protocol import (
    attach_operating_protocol,
    build_operating_protocol,
    render_operating_protocol,
    should_emit_operating_protocol,
)
from odibi_anchor.planning._task_profile import normalize_task_profile
from odibi_anchor.planning._task_render import render_task_execution_report


def _state(profile=None, **overrides):
    values = dict(
        active_task_profile=profile, routing_stale=False, learning_obligation_id=None,
        latest_closed_obligation_id=None, task_verification_epoch=None,
        session_id="session-1", active_project="managed", artifact_root="/artifacts",
        target_root="/target", linked_problem="PRB-1", linked_spec="SPEC.md",
        task_window_id="ltw-1", session_name=None, skills_loaded=set(),
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def test_bootstrap_projection_is_deterministic_bounded_and_hides_task_window():
    state = _state()
    first = build_operating_protocol(state, action="orient")
    second = build_operating_protocol(state, action="orient")
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    assert first["phase"] == "bootstrap"
    assert "task_window_id" not in first["authority"]
    assert first["authority"]["task_active"] is False
    assert first["authority"]["execution_mode"] is None
    assert first["required_now"] == [{
        "id": "orient", "satisfy_with": {"route": "orient"},
    }]
    assert first["effect_authority"]["profile_compatible"] == []
    assert first["effect_authority"]["profile_incompatible"] == [
        "governance_write", "artifact_write", "source_write", "data_write",
    ]
    assert first["effect_authority"]["runtime_prohibited"] == ["external_mutation"]
    assert len(first["required_now"]) <= 20


def test_effect_projection_consumes_same_helper_for_all_modes_and_effects():
    for mode in ("read_only", "artifact_only", "source_change", "data_change"):
        profile = normalize_task_profile(execution_mode=mode)
        protocol = build_operating_protocol(_state(profile), action="task")
        projected = set(protocol["effect_authority"]["profile_compatible"])
        for effect in (
            "read", "governance_write", "artifact_write", "source_write", "data_write",
        ):
            assert (effect in projected) is task_profile_effect_compatible(effect, profile)
        assert protocol["authority"]["task_active"] is True
        assert protocol["authority"]["execution_mode"] == mode


@pytest.mark.parametrize("mode", ["read_only", "artifact_only", "source_change", "data_change"])
@pytest.mark.parametrize("effect", [
    "orient", "read", "artifact_write", "source_write", "data_write", "external_mutation",
])
def test_shared_effect_enforcement_parity_for_every_mode_and_effect(mode, effect):
    profile = normalize_task_profile(execution_mode=mode)
    allowed = effect in {"orient", "read"} or (
        effect == "artifact_write" and mode != "read_only"
    ) or (effect == "source_write" and mode == "source_change") or (
        effect == "data_write" and mode == "data_change"
    )
    if allowed:
        enforce_effects("ordinary_action", (effect,), profile)
    else:
        with pytest.raises(RuntimeError, match="BLOCKED"):
            enforce_effects("ordinary_action", (effect,), profile)


def test_artifact_bookkeeping_exceptions_remain_narrow():
    read_only = normalize_task_profile(execution_mode="read_only")
    for action in ("task", "new_session"):
        enforce_effects(action, ("artifact_write",), None)
    for action in ("checkpoint", "learn", "skill_loaded", "log"):
        enforce_effects(action, ("artifact_write",), read_only)
        with pytest.raises(RuntimeError, match="active task profile"):
            enforce_effects(action, ("artifact_write",), None)
    with pytest.raises(RuntimeError, match="read_only"):
        enforce_effects("save", ("artifact_write",), read_only)


def test_gate_learning_terminal_precedence_and_stale_rebootstrap():
    profile = normalize_task_profile(execution_mode="source_change")
    timings = [{"action": "gate", "passed": True}]
    learning = build_operating_protocol(
        _state(profile, learning_obligation_id="lob-1", task_verification_epoch=0),
        action="gate", session_timings=timings,
    )
    assert learning["phase"] == "learning"
    terminal = build_operating_protocol(
        _state(profile, latest_closed_obligation_id="lob-1", task_verification_epoch=0),
        action="learning", session_timings=timings,
    )
    assert terminal["phase"] == "terminal"
    assert terminal["terminal_return"]["status"] == "ready"
    stale = build_operating_protocol(_state(profile, routing_stale=True), action="project")
    assert stale["phase"] == "rebootstrap" and stale["rebootstrap_required"] is True


def test_recovered_learning_debt_without_active_task_remains_in_progress():
    protocol = build_operating_protocol(
        _state(learning_obligation_id="lob-1"), action="orient",
    )
    assert protocol["phase"] == "learning"
    assert protocol["required_now"] == [{
        "id": "learning_closure", "satisfy_with": {"route": "learning.assess"},
    }]
    assert protocol["terminal_return"] == {
        "status": "in_progress", "basis": "lifecycle_incomplete",
    }


def test_failed_checkpoint_transition_is_verification_even_without_timing_commit():
    profile = normalize_task_profile(execution_mode="source_change")
    protocol = build_operating_protocol(
        _state(profile, task_verification_epoch=0,
               latest_closed_obligation_id="lob-1"), action="checkpoint",
        result={"metrics": {"overall_pass": False, "failed_at": "pr_draft"}},
        session_timings=[{"action": "gate", "error": None, "passed": True}],
    )
    assert protocol["phase"] == "verification"
    assert protocol["verification_obligations"][0] == {
        "id": "task_window_gate", "status": "pending",
    }
    assert protocol["terminal_return"]["status"] == "in_progress"


def test_latest_failed_gate_timing_masks_an_earlier_pass():
    profile = normalize_task_profile(execution_mode="source_change")
    protocol = build_operating_protocol(
        _state(profile, task_verification_epoch=0,
               latest_closed_obligation_id="lob-1"), action="learning",
        session_timings=[
            {"action": "gate", "error": None, "passed": True},
            {"action": "gate", "error": None, "passed": False},
        ],
    )
    assert protocol["phase"] == "verification"
    assert protocol["verification_obligations"][0]["status"] == "pending"
    assert protocol["terminal_return"]["status"] == "in_progress"


def test_emission_predicate_and_shared_markdown_are_conservative():
    assert should_emit_operating_protocol("new_session")
    assert should_emit_operating_protocol("status")
    assert should_emit_operating_protocol("learning", ("capture",))
    assert should_emit_operating_protocol("problem", ("create",))
    assert should_emit_operating_protocol("snapshot", kwargs={"mode": "handoff"})
    assert not should_emit_operating_protocol("snapshot", kwargs={"mode": "full"})
    assert not should_emit_operating_protocol("problem", ("show",))
    assert not should_emit_operating_protocol("checkpoint", checkpoint_nested=True)
    rendered = render_operating_protocol(build_operating_protocol(_state(), action="orient"))
    assert "not permission" in rendered
    assert "external_mutation" in rendered
    assert "owner" not in rendered.lower()


def test_every_real_mutating_selector_and_representative_reads():
    selectors = {
        "problem": {"create", "update", "link_spec", "close"},
        "spec": {"create", "done", "persist", "execute", "review", "from_problem"},
        "work_item": {"create", "update", "approve", "record_publish", "close"},
    }
    for action, values in selectors.items():
        assert all(should_emit_operating_protocol(action, (value,)) for value in values)
        assert not should_emit_operating_protocol(action, ("show",))
    for nonexistent in ("advance", "transition", "defer"):
        assert not any(should_emit_operating_protocol(action, (nonexistent,)) for action in selectors)


def test_state_supported_verification_never_promotes_missing_evidence():
    profile = normalize_task_profile(execution_mode="source_change")
    protocol = build_operating_protocol(_state(profile), action="gate", session_timings=[])
    statuses = {row["id"]: row["status"] for row in protocol["verification_obligations"]}
    assert statuses == {
        "task_window_gate": "unknown", "linked_spec": "satisfied",
        "persisted_spec": "pending", "reviewed_spec": "unknown",
        "preflight": "unknown", "test": "unknown", "review": "unknown",
    }
    evidenced = build_operating_protocol(
        _state(profile, spec_persisted=True, persisted_spec_name="SPEC.md",
               spec_review_rating="good", reviewed_spec_name="SPEC.md",
               task_verification_epoch=0), action="gate",
        session_timings=[{"action": name, "error": None, "passed": True}
                         for name in ("preflight", "test", "review")],
    )
    assert all(row["status"] == "satisfied" for row in evidenced["verification_obligations"][1:])


def test_latest_verification_result_and_review_rating_fail_conservatively():
    profile = normalize_task_profile(execution_mode="source_change")
    protocol = build_operating_protocol(
        _state(profile, spec_review_rating="poor", reviewed_spec_name="SPEC.md",
               task_verification_epoch=0), action="gate",
        session_timings=[
            {"action": "test", "error": None, "passed": True},
            {"action": "test", "error": None, "passed": False},
        ],
    )
    statuses = {row["id"]: row["status"] for row in protocol["verification_obligations"]}
    assert statuses["test"] == "pending"
    assert statuses["reviewed_spec"] == "pending"


def test_attach_does_not_duplicate_hard_suggestions():
    protocol = build_operating_protocol(_state(session_name="bounded"), action="orient")
    result = {"suggested_next_actions": []}
    attach_operating_protocol(result, protocol)
    attach_operating_protocol(result, protocol)
    assert result["suggested_next_actions"] == ["MUST: satisfy accept_task via task."]


def test_attach_repairs_non_list_suggestions_at_the_additive_boundary():
    protocol = build_operating_protocol(_state(), action="orient")
    result = {"suggested_next_actions": None}
    attach_operating_protocol(result, protocol)
    assert result["suggested_next_actions"] == ["MUST: satisfy orient via orient."]


def test_agent_context_attachment_is_compact_and_points_to_required_skill():
    state = _state(normalize_task_profile(execution_mode="source_change"))
    protocol = build_operating_protocol(state, action="task")
    protocol["required_now"] = [{
        "id": "load_required_skill",
        "satisfy_with": {"route": "skill_loaded", "skill": "writing-specs"},
    }]
    result = {"kind": "task_execution_context"}

    attach_agent_context(result, state, protocol=protocol)

    context = result["agent_context"]
    assert tuple(context["facts"]) == ("project", "task", "lifecycle")
    assert context["next_operation"]["copy_ready"] == 'anchor("skill_loaded", "writing-specs")'
    assert context["resource_pointers"] == [{
        "kind": "skill",
        "id": "writing-specs",
        "path": "skills/writing-specs/SKILL.md",
        "reason": "required by the current accepted-task policy",
        "status": "derived",
        "provenance": {"source": "operating_protocol.required_now"},
    }]


def test_public_task_renderer_renders_protocol_once():
    protocol = build_operating_protocol(
        _state(normalize_task_profile(execution_mode="artifact_only"), session_name="bounded"),
        action="task",
    )
    context = {
        "kind": "task_execution_context", "subject": "bounded task",
        "summary": "ready", "status": "ready",
        "mode": "planning", "readiness": {"score": 100}, "canonical_guidance": [],
        "artifact_contract": {}, "context_plan": {}, "guidance_obligations": [],
        "intent": {}, "background": {}, "scope": {}, "resources": {}, "context": {},
        "constraints": [], "discovery": {}, "plan": [], "critique_checks": [],
        "verification": {}, "guardrails": {}, "risks": [], "hints": {}, "findings": [],
        "required_skills": [], "suggested_next_actions": [], "operating_protocol": protocol,
    }
    rendered = render_task_execution_report(context)
    assert rendered.count("## Operating Protocol v1") == 1
    assert rendered.count("### Verification obligations") == 1
