"""Skill-load gate covers substantive work actions, not just file edits.

Closes the read-only/analysis hole: once a task mode is active, work actions
(profile_table, quality, map, …) require the mode's skills loaded — the pre-action
skill layer now has the same teeth as touched/safe/semantic.
"""
from __future__ import annotations

from odibi_anchor._dispatcher._enforcement import should_block_skill_gate
from odibi_anchor.planning._task_builders import (
    _SKILL_GATE_EXEMPT,
    required_skills_for_task,
)
from odibi_anchor.planning._task_profile import normalize_task_profile


def _block(action, mode, loaded):
    profile = normalize_task_profile(
        legacy_mode=mode or None,
        domains=(("code",) if mode == "analysis" else None),
    )
    requirements = required_skills_for_task(profile)
    return should_block_skill_gate(
        action, mode, set(loaded), {mode: requirements}, _SKILL_GATE_EXEMPT
    )


class TestWorkActionsGated:
    def test_readonly_work_blocked_without_skills(self):
        # the bug Genie hit: analysis task, then profile a table, skills never loaded
        blocked, msg = _block("profile_table", "analysis", [])
        assert blocked
        assert "code-comprehension" in msg

    def test_work_allowed_once_skills_loaded(self):
        blocked, _ = _block("profile_table", "analysis", ["code-comprehension"])
        assert not blocked

    def test_codebase_work_gated_too(self):
        # map/impact/consistency are comprehension work -> gated in analysis mode
        for action in ("map", "impact", "consistency"):
            blocked, _ = _block(action, "analysis", [])
            assert blocked, f"{action} should require skills in analysis mode"

    def test_data_mode_gates_data_tools(self):
        profile = normalize_task_profile(
            legacy_mode="implementation", execution_mode="data_change",
        )
        requirements = {"implementation": required_skills_for_task(profile)}
        blocked, msg = should_block_skill_gate(
            "pre_join", "implementation", set(), requirements, _SKILL_GATE_EXEMPT
        )
        assert blocked
        assert "data-operations" in msg


class TestExemptActions:
    def test_orientation_and_compliance_never_blocked(self):
        # these run before skills are loaded / are the compliance machinery itself
        for action in ("status", "memory", "audit_history", "task", "orient",
                       "skill_loaded", "gate", "review", "learn", "save", "help",
                       "context", "prepare"):
            blocked, _ = _block(action, "analysis", [])
            assert not blocked, f"{action} must be exempt from the skill gate"

    def test_no_mode_no_block(self):
        # before any task() sets a mode, the gate no-ops (pre-plan exploration ok)
        blocked, _ = _block("profile_table", None, [])
        assert not blocked


class TestExemptSetSanity:
    def test_work_actions_not_exempt(self):
        # guard against accidentally exempting real work
        for action in ("profile_table", "quality", "pre_join", "transform",
                       "investigate", "map", "touched", "safe", "semantic"):
            assert action not in _SKILL_GATE_EXEMPT

    def test_legacy_fallback_when_no_exempt_set(self):
        # direct callers passing no exempt set keep the old file-edit-only behavior
        profile = normalize_task_profile(legacy_mode="analysis", domains=("code",))
        requirements = {"analysis": required_skills_for_task(profile)}
        blocked, _ = should_block_skill_gate("profile_table", "analysis", set(), requirements)
        assert not blocked  # not a file edit -> not gated under legacy fallback
        blocked, _ = should_block_skill_gate("touched", "analysis", set(), requirements)
        assert blocked
