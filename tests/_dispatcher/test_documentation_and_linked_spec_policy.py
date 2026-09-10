"""Focused policy tests for documentation paths and exact linked-spec evidence."""

from types import SimpleNamespace

import pytest

from odibi_anchor._dispatcher._effects import build_static_action_contracts, enforce_effects
from odibi_anchor._dispatcher._enforcement import (
    should_block_documentation_paths,
    should_block_spec_gate,
    should_block_spec_review_gate,
)
from odibi_anchor._dispatcher._post_dispatch import run_post_dispatch
from odibi_anchor._dispatcher._pre_dispatch import run_pre_dispatch_enforcement
from odibi_anchor._dispatcher._spec import spec_action
from odibi_anchor._dispatcher._task_injection import inject_session_context
from odibi_anchor._utils._session_state import SessionState
from odibi_anchor.planning._task_builders import required_skills_for_task
from odibi_anchor.planning._task_profile import normalize_task_profile
from odibi_anchor.planning.task_execution_context import (
    _build_required_skills,
    task_execution_context,
)


@pytest.mark.parametrize("path", ["README.md", "guide.MARKDOWN", "a/b.Md"])
def test_documentation_policy_allows_only_markdown(path):
    assert should_block_documentation_paths("documentation", [path]) == (False, "")


@pytest.mark.parametrize("path", ["x.py", "x.mdx", "x.txt", "README", "x.yaml"])
def test_documentation_policy_rejects_every_other_extension(path):
    assert should_block_documentation_paths("documentation", [path])[0]
    assert not should_block_documentation_paths("implementation", [path])[0]


def test_documentation_mode_registry_and_readiness_contract():
    result = task_execution_context(
        "Update README.md", goal="Clarify setup", mode="documentation",
        in_scope=["README.md"],
    )
    assert result["task_profile"] == {
        "schema_version": "1.0", "work_type": "communicate",
        "execution_mode": "artifact_only", "risk": "low", "rigor": "direct",
        "domains": ["general"], "traits": ["documentation"],
        "caller_required_evidence": [], "legacy_mode": "documentation",
        "normalization_notes": ["normalized legacy mode 'documentation'"],
    }
    profile = normalize_task_profile(legacy_mode="documentation")
    assert required_skills_for_task(profile) == ("documentation",)
    assert [item["skill"] for item in result["required_skills"]] == ["documentation"]


def test_task_required_skills_follow_canonical_specification_policy():
    ordinary = task_execution_context(
        "Fix one parser projection", goal="Keep output truthful",
        mode="implementation", rigor="direct",
    )
    high_risk = task_execution_context(
        "Change a public contract", goal="Ship a guarded migration",
        mode="implementation", risk="high",
    )

    assert "spec_required" not in {item["skill"] for item in ordinary["required_skills"]}
    assert "writing-specs" in {item["skill"] for item in high_risk["required_skills"]}


def test_explicit_policy_requirement_selects_writing_specs_independent_of_mode():
    for mode in ("planning", "implementation", "migration", "data"):
        profile = normalize_task_profile(legacy_mode=mode)
        skills = _build_required_skills(profile, specification_disposition="required")
        assert "writing-specs" in {item["skill"] for item in skills}


def test_ordinary_planning_does_not_select_writing_specs():
    profile = normalize_task_profile(legacy_mode="planning")
    assert "writing-specs" not in required_skills_for_task(profile)


def _run_touched_pre_dispatch(state, tmp_path):
    return run_pre_dispatch_enforcement(
        "touched", ("README.md",), {}, session_state=state,
        session_timings=[{"action": "known_bad", "error": None}],
        session_files_changed=set(), session_boot_manifest={},
        planning_required_actions=frozenset(), root=str(tmp_path),
        action_contract=build_static_action_contracts()["touched"],
    )


def test_ordinary_implementation_is_not_hard_blocked_without_spec(tmp_path):
    state = SessionState(active_task_mode="implementation")
    state.active_task_profile = normalize_task_profile(legacy_mode="implementation")
    state.skills_loaded.update(required_skills_for_task(state.active_task_profile))

    assert _run_touched_pre_dispatch(state, tmp_path) is None


def test_required_implementation_keeps_exact_persisted_reviewed_spec_gate(tmp_path):
    state = SessionState(active_task_mode="implementation")
    state.active_task_profile = normalize_task_profile(
        legacy_mode="implementation", risk="high",
    )
    state.skills_loaded.update(required_skills_for_task(
        state.active_task_profile, specification_disposition="required",
    ))

    with pytest.raises(RuntimeError, match="requires a specification"):
        _run_touched_pre_dispatch(state, tmp_path)

    state.spec_persisted = True
    state.linked_spec = state.persisted_spec_name = state.reviewed_spec_name = "GUARDED"
    state.spec_review_rating = "good"

    assert _run_touched_pre_dispatch(state, tmp_path) is None


def test_documentation_touched_rejects_non_markdown_before_ledger_registration(tmp_path):
    ledger = set()
    state = SimpleNamespace(active_task_mode="documentation")
    with pytest.raises(RuntimeError, match="Documentation mode"):
        run_pre_dispatch_enforcement(
            "touched", ("settings.json",), {}, session_state=state,
            session_timings=[], session_files_changed=ledger,
            session_boot_manifest={}, planning_required_actions=frozenset(),
            root=str(tmp_path), action_contract=None,
        )
    assert ledger == set()


@pytest.mark.parametrize("effect", ["source_write", "data_write"])
def test_documentation_profile_denies_source_and_data_writes(effect):
    profile = task_execution_context(
        "Update README.md", goal="Clarify setup", mode="documentation",
        in_scope=["README.md"],
    )["task_profile"]
    from odibi_anchor.planning._task_profile import TaskProfile
    with pytest.raises(RuntimeError, match="incompatible"):
        enforce_effects("test-action", frozenset({effect}), TaskProfile.from_dict(profile))


def test_exact_linked_spec_legacy_helpers_keep_identity_semantics():
    required_modes = frozenset({"implementation"})
    assert should_block_spec_gate(
        "touched", "implementation", True, required_modes,
    )[0]
    assert should_block_spec_review_gate(
        "touched", "implementation", True, "excellent", required_modes,
    )[0]
    assert should_block_spec_gate(
        "touched", "implementation", True, required_modes, "A", "B",
    )[0]
    assert not should_block_spec_gate(
        "touched", "implementation", True, required_modes, "A", "A",
    )[0]
    assert should_block_spec_review_gate(
        "touched", "implementation", True, "excellent", required_modes,
        "A", "A", "B",
    )[0]
    assert not should_block_spec_review_gate(
        "touched", "implementation", True, "excellent", required_modes,
        "A", "A", "A",
    )[0]


def _post(state, command, result):
    run_post_dispatch(
        "spec", result, None, (command,), {}, session_timings=[],
        session_files_changed=set(), session_state=state,
        planning_required_actions=frozenset(),
    )


def test_spec_create_links_but_does_not_count_as_persisted_and_rebinds_review():
    state = SessionState(linked_spec="OLD", persisted_spec_name="OLD",
                         reviewed_spec_name="OLD", spec_review_rating="excellent")
    _post(state, "create", {
        "created": True, "write_performed": True, "path": "/x/NEW_SPEC.md", "name": "new",
    })
    assert state.linked_spec == "NEW"
    assert state.persisted_spec_name == "OLD"
    assert state.spec_persisted is False
    assert state.reviewed_spec_name is None
    assert state.spec_review_rating is None


def test_persist_and_review_are_bound_to_canonical_exact_name_without_task_reset():
    state = SessionState(active_task_mode="implementation")
    _post(state, "persist", {
        "created": True, "write_performed": True, "path": "/x/My-Feature_SPEC.md",
        "name": "My-Feature_SPEC.md",
    })
    assert state.linked_spec == state.persisted_spec_name == "MY_FEATURE"
    _post(state, "review", {
        "write_performed": True, "spec_name": "my feature", "rating": "good",
    })
    assert state.reviewed_spec_name == "MY_FEATURE"
    assert state.active_task_mode == "implementation"


def test_pending_executed_spec_is_injected_into_followup_task(monkeypatch, tmp_path):
    state = SessionState(pending_task_spec_name="ACTIVE")
    monkeypatch.setitem(
        inject_session_context.__globals__, "_resolve_and_parse_spec",
        lambda name, specs_dir: {
            "name": name, "status": "executing", "phases": [],
            "success_criteria": ["works"],
        },
    )
    result = inject_session_context(
        {"mode": "implementation", "goal": "Implement active spec"},
        None, set(), False, specs_dir=tmp_path, session_state=state,
    )
    assert result["_task_policy_stage"]["linked_spec"] == "ACTIVE"
    assert result["_task_policy_stage"]["persisted_spec_name"] == "ACTIVE"
    assert "[spec] works" in result["acceptance_criteria"]


def test_accepted_task_consumes_pending_identity_and_clears_review(monkeypatch, tmp_path):
    state = SessionState(
        pending_task_spec_name="ACTIVE", reviewed_spec_name="OLD",
        spec_review_rating="excellent",
    )
    monkeypatch.setitem(
        inject_session_context.__globals__, "_resolve_and_parse_spec",
        lambda name, specs_dir: {
            "name": name, "status": "executing", "phases": [],
            "success_criteria": ["works"],
        },
    )
    injected = inject_session_context(
        {"mode": "implementation", "goal": "Implement active spec"},
        None, set(), False, specs_dir=tmp_path, session_state=state,
    )
    stage = injected["_task_policy_stage"]
    monkeypatch.setattr(
        "odibi_anchor._repository_snapshot.capture_task_repository_baseline",
        lambda *_args, **_kwargs: {},
    )
    state.target_root = state.artifact_root = str(tmp_path)
    run_post_dispatch(
        "task", {"readiness": {"score": 100}, "task_profile": stage["profile"].to_dict()},
        None, ("Implement active spec",), {"mode": "implementation"},
        session_timings=[], session_files_changed=set(), session_state=state,
        planning_required_actions=frozenset(), task_stage=stage,
    )
    assert state.pending_task_spec_name is None
    assert state.linked_spec == state.persisted_spec_name == "ACTIVE"
    assert state.spec_persisted is True
    assert state.reviewed_spec_name is None
    assert state.spec_review_rating is None

    later = inject_session_context(
        {"mode": "implementation", "goal": "A later unrelated task"},
        None, set(), False, specs_dir=tmp_path, session_state=state,
    )
    assert later["_task_policy_stage"]["linked_spec"] is None


def test_readiness_rejection_does_not_consume_pending_spec():
    state = SessionState(pending_task_spec_name="ACTIVE")
    with pytest.raises(RuntimeError, match="readiness too low"):
        run_post_dispatch(
            "task", {"readiness": {"score": 0, "missing_details": ["goal"]}},
            None, ("unfinished",), {"mode": "implementation"},
            session_timings=[], session_files_changed=set(), session_state=state,
            planning_required_actions=frozenset(), task_stage=None,
        )
    assert state.pending_task_spec_name == "ACTIVE"


def test_explicit_resolved_spec_stages_persistence_without_prior_review(monkeypatch, tmp_path):
    state = SessionState(reviewed_spec_name="OLD", spec_review_rating="excellent")
    monkeypatch.setitem(
        inject_session_context.__globals__, "_resolve_and_parse_spec",
        lambda name, specs_dir: {
            "name": "EXPLICIT", "status": "ready", "phases": [],
            "success_criteria": ["works"],
        },
    )
    injected = inject_session_context(
        {"mode": "implementation", "goal": "Implement explicit spec", "spec": "explicit"},
        None, set(), False, specs_dir=tmp_path, session_state=state,
    )
    stage = injected["_task_policy_stage"]
    monkeypatch.setattr(
        "odibi_anchor._repository_snapshot.capture_task_repository_baseline",
        lambda *_args, **_kwargs: {},
    )
    state.target_root = state.artifact_root = str(tmp_path)
    run_post_dispatch(
        "task", {"readiness": {"score": 100}, "task_profile": stage["profile"].to_dict()},
        None, ("Implement explicit spec",), {"mode": "implementation"},
        session_timings=[], session_files_changed=set(), session_state=state,
        planning_required_actions=frozenset(), task_stage=stage,
    )
    assert state.linked_spec == state.persisted_spec_name == "EXPLICIT"
    assert state.spec_persisted is True
    assert state.reviewed_spec_name is None
    assert state.spec_review_rating is None


def test_real_execute_binds_exactly_one_accepted_followup_task(monkeypatch, tmp_path):
    specs_dir = tmp_path / "specs"
    specs_dir.mkdir()
    (specs_dir / "ACTIVE_SPEC.md").write_text(
        "---\n"
        "status: ready\n"
        "complexity: low\n"
        "estimated_sessions: 1\n"
        "phases: []\n"
        "success_criteria:\n"
        "  - works\n"
        "files_touched: []\n"
        "permissions_needed: []\n"
        "---\n\n# Active\n",
        encoding="utf-8",
    )
    execute_result = spec_action(
        tmp_path, "execute", "ACTIVE", specs_dir=specs_dir, output_format="dict",
    )
    assert execute_result["executing"] is True

    state = SessionState()
    _post(state, "execute", execute_result)
    assert state.pending_task_spec_name == "ACTIVE"

    injected = inject_session_context(
        {"mode": "implementation", "goal": "Implement active spec"},
        None, set(), False, specs_dir=specs_dir, session_state=state,
    )
    stage = injected["_task_policy_stage"]
    monkeypatch.setattr(
        "odibi_anchor._repository_snapshot.capture_task_repository_baseline",
        lambda *_args, **_kwargs: {},
    )
    state.target_root = state.artifact_root = str(tmp_path)
    run_post_dispatch(
        "task", {"readiness": {"score": 100}, "task_profile": stage["profile"].to_dict()},
        None, ("Implement active spec",), {"mode": "implementation"},
        session_timings=[], session_files_changed=set(), session_state=state,
        planning_required_actions=frozenset(), task_stage=stage,
    )
    assert state.linked_spec == state.persisted_spec_name == "ACTIVE"
    assert state.pending_task_spec_name is None

    later = inject_session_context(
        {"mode": "implementation", "goal": "Unrelated later task"},
        None, set(), False, specs_dir=specs_dir, session_state=state,
    )
    assert later["_task_policy_stage"]["linked_spec"] is None
