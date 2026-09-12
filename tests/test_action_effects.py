"""Focused Phase 1 action-effect contract tests."""

from types import SimpleNamespace
from typing import cast

import pytest

from odibi_anchor._dispatcher._effects import (
    BUILTIN_ACTION_NAMES,
    ActionContract,
    ActionEffect,
    InvocationSemantics,
    build_static_action_contracts,
    enforce_effects,
    maximal_effects,
    resolve_effects,
    resolve_invocation,
)
from odibi_anchor._dispatcher._project import project_action
from odibi_anchor.planning._task_profile import normalize_task_profile

CONTRACTS = build_static_action_contracts()


def resolved(action, *args, **kwargs):
    outcome = resolve_invocation(CONTRACTS[action], args, kwargs)
    if outcome.error is not None:
        raise outcome.error
    return outcome.effects[0]


def test_every_static_action_has_non_empty_contract():
    assert set(CONTRACTS) == set(BUILTIN_ACTION_NAMES)
    assert len(CONTRACTS) == 93
    assert all(contract.allowed_effects for contract in CONTRACTS.values())
    assert all(contract.allowed_pre_task_access for contract in CONTRACTS.values())
    assert all(contract.resolve_invocation is not None for contract in CONTRACTS.values())


def test_exact_pre_task_access_assignment_for_all_builtins():
    safe = {
        "audit_history", "context", "help", "new_session", "orient", "prepare", "quick", "skill_loaded",
        "skills", "references", "status", "task", "task_adoption", "tools",
    }
    context = {
        "convention", "frame", "import_resolve", "lookup", "manifest", "map",
        "memory_stats", "memory_tags", "session_delta", "session_diff", "session_files",
        "session_log", "task_rebind",
    }
    mixed = {
        "concurrency", "config", "learning", "memory", "problem", "project", "spec",
        "work_item",
    }
    task = set(BUILTIN_ACTION_NAMES) - safe - context - mixed
    for name in safe:
        assert CONTRACTS[name].allowed_pre_task_access == {"safe_orientation"}
    for name in context:
        assert CONTRACTS[name].allowed_pre_task_access == {"context_collection"}
    for name in task:
        assert CONTRACTS[name].allowed_pre_task_access == {"task_required"}
    assert set(CONTRACTS) == safe | context | mixed | task


@pytest.mark.parametrize("action,selector,effect,access", [
    *[("problem", value, "read", "context_collection") for value in (None, "list", "status", "show", "resume")],
    *[("problem", value, "artifact_write", "task_required") for value in ("create", "update", "link_spec", "close")],
    *[("spec", value, "read", "context_collection") for value in (None, "list", "status")],
    ("spec", "validate", "read", "task_required"),
    *[("spec", value, "artifact_write", "task_required") for value in ("create", "done", "persist", "execute", "review", "from_problem")],
    *[("work_item", value, "read", "context_collection") for value in (None, "list", "status", "show")],
    ("work_item", "preview", "read", "task_required"),
    *[("work_item", value, "artifact_write", "task_required") for value in ("create", "update", "approve", "record_publish", "close")],
    *[("learning", value, "read", "context_collection") for value in (None, "list", "show", "insights", "export")],
    *[("learning", value, "governance_write", "task_required") for value in ("capture", "assess", "safe_stop")],
    *[("learning", value, "artifact_write", "task_required") for value in ("triage", "backup")],
    *[("memory", value, "governance_write", "task_required") for value in ("apply", "disposition", "evaluate")],
    *[("project", value, "read", "safe_orientation") for value in (None, "list", "status")],
    *[("project", value, "artifact_write", "task_required") for value in ("create", "set_target")],
])
def test_every_valid_mixed_selector_resolves_both_axes(action, selector, effect, access):
    args = () if selector is None else (selector,)
    outcome = resolve_invocation(CONTRACTS[action], args, {})
    assert (outcome.effects, outcome.pre_task_access, outcome.error) == ((effect,), access, None)


@pytest.mark.parametrize("action,kwargs,effect,access", [
    ("config", {}, "read", "context_collection"),
    ("config", {"suppress_id": "R1"}, "artifact_write", "task_required"),
    ("project", {"action": "migrate", "dry_run": True}, "read", "task_required"),
    ("project", {"action": "migrate", "dry_run": False}, "artifact_write", "task_required"),
    ("contract", {"save": False}, "read", "task_required"),
    ("contract", {"save": True}, "artifact_write", "task_required"),
    ("apply_sql", {"mode": "view"}, "data_write", "task_required"),
    ("apply_sql", {"mode": "table"}, "data_write", "task_required"),
    ("apply_transform", {"dry_run": True}, "read", "task_required"),
    ("apply_transform", {"dry_run": False}, "data_write", "task_required"),
    ("dogfood", {"save_as_baseline": False}, "read", "task_required"),
    ("dogfood", {"save_as_baseline": True}, "artifact_write", "task_required"),
    ("memory_hygiene", {"dry_run": True}, "read", "task_required"),
    ("memory_hygiene", {"dry_run": False}, "artifact_write", "task_required"),
    ("observe_table", {"persist": False}, "read", "task_required"),
    ("observe_table", {"persist": True}, "artifact_write", "task_required"),
    ("concurrency", {"command": "inspect"}, "read", "context_collection"),
    ("concurrency", {"command": "dry_run"}, "read", "context_collection"),
    ("concurrency", {"command": "apply"}, "artifact_write", "task_required"),
    ("concurrency", {"command": "rollback"}, "artifact_write", "task_required"),
])
def test_every_flag_sensitive_contract_resolves_both_axes(action, kwargs, effect, access):
    outcome = resolve_invocation(CONTRACTS[action], (), kwargs)
    assert (outcome.effects, outcome.pre_task_access, outcome.error) == ((effect,), access, None)


@pytest.mark.parametrize("action", ["problem", "spec", "work_item", "project"])
@pytest.mark.parametrize("selector", ["unknown", object()])
def test_every_mixed_selector_family_fails_closed(action, selector):
    outcome = resolve_invocation(CONTRACTS[action], (selector,), {})
    assert outcome.pre_task_access == "task_required"
    assert outcome.error is not None


def test_fixed_builtin_effect_sets_match_independent_contract_table():
    expected = {
        "source_write": {"safe", "semantic"},
        "artifact_write": {
            "task", "checkpoint", "learn", "save", "confirm", "reject", "snapshot",
            "save_snap", "archive", "import_md", "db_migrate", "register_tool",
            "touched", "skill_loaded", "log", "new_session", "incident_snapshot",
            "task_adoption", "task_rebind",
        },
        "external_mutation": {"sync"},
        "orient": {"orient", "help", "quick"},
    }
    polymorphic = {
        "problem", "spec", "work_item", "project", "config", "contract", "apply_sql",
        "apply_transform", "dogfood", "learning", "memory", "memory_hygiene", "observe_table",
        "concurrency",
    }
    classified = polymorphic.copy()
    for effect, actions in expected.items():
        classified.update(actions)
        for action in actions:
            assert CONTRACTS[action].allowed_effects == {effect}
            assert resolved(action) == effect
    remaining = set(BUILTIN_ACTION_NAMES) - classified
    for action in remaining:
        assert CONTRACTS[action].allowed_effects == {"read"}
        assert resolved(action) == "read"


@pytest.mark.parametrize("selector", ["list", "show", "insights", "export"])
def test_learning_read_effects(selector):
    outcome = resolve_invocation(CONTRACTS["learning"], (selector,), {})
    assert (outcome.effects, outcome.pre_task_access) == (("read",), "context_collection")


@pytest.mark.parametrize("selector", ["capture", "assess", "safe_stop"])
def test_learning_write_effects(selector):
    outcome = resolve_invocation(CONTRACTS["learning"], (selector,), {})
    assert (outcome.effects, outcome.pre_task_access) == (("governance_write",), "task_required")


@pytest.mark.parametrize("selector", ["triage", "backup"])
def test_learning_artifact_write_effects(selector):
    outcome = resolve_invocation(CONTRACTS["learning"], (selector,), {})
    assert (outcome.effects, outcome.pre_task_access) == (("artifact_write",), "task_required")


@pytest.mark.parametrize("selector", [None, "diagnostics", "seed", "task_record", "replay", "storage"])
def test_memory_read_effects(selector):
    args = () if selector is None else (selector,)
    outcome = resolve_invocation(CONTRACTS["memory"], args, {})
    assert (outcome.effects, outcome.pre_task_access) == (("read",), "context_collection")


@pytest.mark.parametrize("selector", ["apply", "disposition", "evaluate"])
def test_memory_write_effects(selector):
    outcome = resolve_invocation(CONTRACTS["memory"], (selector,), {})
    assert (outcome.effects, outcome.pre_task_access) == (("governance_write",), "task_required")


def test_reviewed_seed_load_is_a_pre_task_artifact_write():
    outcome = resolve_invocation(CONTRACTS["memory"], ("seed",), {"command": "load"})
    assert (outcome.effects, outcome.pre_task_access) == (("artifact_write",), "context_collection")


def test_memory_promotion_effect_depends_on_shadow_command():
    inspected = resolve_invocation(
        CONTRACTS["memory"], ("promotion",), {"command": "inspect"},
    )
    evaluated = resolve_invocation(
        CONTRACTS["memory"], ("promotion",), {"command": "evaluate"},
    )
    verified = resolve_invocation(
        CONTRACTS["memory"], ("promotion",), {"command": "verify"},
    )
    attested = resolve_invocation(
        CONTRACTS["memory"], ("promotion",), {"command": "attest"},
    )
    withdrawn = resolve_invocation(
        CONTRACTS["memory"], ("promotion",), {"command": "withdraw"},
    )
    owner_requested = resolve_invocation(
        CONTRACTS["memory"], ("promotion",), {"command": "request_owner_activation"},
    )
    assert (inspected.effects, inspected.pre_task_access) == (("read",), "context_collection")
    assert (evaluated.effects, evaluated.pre_task_access) == (("artifact_write",), "task_required")
    assert (verified.effects, verified.pre_task_access) == (("artifact_write",), "task_required")
    assert (attested.effects, attested.pre_task_access) == (("artifact_write",), "task_required")
    assert (withdrawn.effects, withdrawn.pre_task_access) == (("artifact_write",), "task_required")
    assert (owner_requested.effects, owner_requested.pre_task_access) == (
        ("artifact_write",), "task_required",
    )


@pytest.mark.parametrize("selector", [None, "list", "status", "show", "resume"])
def test_problem_read_selectors(selector):
    assert resolved("problem", *(() if selector is None else (selector,))) == "read"


@pytest.mark.parametrize("selector", ["create", "update", "link_spec", "close"])
def test_problem_write_selectors(selector):
    assert resolved("problem", selector) == "artifact_write"


@pytest.mark.parametrize("selector", [None, "list", "status", "validate"])
def test_spec_read_selectors(selector):
    assert resolved("spec", *(() if selector is None else (selector,))) == "read"


@pytest.mark.parametrize("selector", ["create", "done", "persist", "execute", "review", "from_problem"])
def test_spec_write_selectors(selector):
    assert resolved("spec", selector) == "artifact_write"


@pytest.mark.parametrize("selector", [None, "list", "status", "show", "preview"])
def test_work_item_read_selectors(selector):
    assert resolved("work_item", *(() if selector is None else (selector,))) == "read"


@pytest.mark.parametrize("selector", ["create", "update", "approve", "record_publish", "close"])
def test_work_item_write_selectors(selector):
    assert resolved("work_item", selector) == "artifact_write"


def test_observe_table_is_an_artifact_write_only_when_persisting():
    assert resolved("observe_table") == "read"
    assert resolved("observe_table", persist=True) == "artifact_write"
    with pytest.raises(ValueError, match="must be boolean"):
        resolved("observe_table", persist=1)


@pytest.mark.parametrize("selector", [None, "list", "status"])
def test_project_read_selectors(selector):
    assert resolved("project", *(() if selector is None else (selector,))) == "read"


@pytest.mark.parametrize("selector", ["create", "set_target"])
def test_project_write_selectors(selector):
    assert resolved("project", selector) == "artifact_write"


def test_exact_no_write_project_selection_is_safe_but_grants_no_later_effects(tmp_path):
    project_action(tmp_path, "create", name="Alpha Project", output_format="dict")
    project_action(tmp_path, "create", name="Beta Project", output_format="dict")
    project_action(tmp_path, "use", "alpha-project", output_format="dict")
    contract = build_static_action_contracts(anchor_home=tmp_path)["project"]

    selection = resolve_invocation(
        contract,
        ("use", "alpha-project"),
        {"output_format": "dict"},
    )

    assert (selection.effects, selection.pre_task_access, selection.error) == (
        ("read",),
        "safe_orientation",
        None,
    )
    enforce_effects("project", selection.effects, None)
    for action, effect in (
        ("save", "artifact_write"),
        ("safe", "source_write"),
        ("apply_sql", "data_write"),
        ("sync", "external_mutation"),
    ):
        with pytest.raises(RuntimeError):
            enforce_effects(action, (cast(ActionEffect, effect),), None)


def test_missing_ambiguous_fuzzy_or_mutating_project_use_retains_artifact_write(tmp_path):
    project_action(tmp_path, "create", name="Alpha Project", output_format="dict")
    project_action(tmp_path, "create", name="Beta Project", output_format="dict")
    project_action(tmp_path, "use", "alpha-project", output_format="dict")
    beta_descriptor = tmp_path / "workspace" / "projects" / "beta-project" / "PROJECT.md"
    beta_descriptor.write_text(
        beta_descriptor.read_text(encoding="utf-8").replace(
            "name: Beta Project",
            "name: Alpha Project",
        ),
        encoding="utf-8",
    )
    contract = build_static_action_contracts(anchor_home=tmp_path)["project"]
    unsafe_invocations = (
        (("use", "missing"), {}),
        (("use", "Alpha Project"), {}),
        (("use", "ALPHA PROJECT"), {}),
        (("use", "beta-project"), {}),
        (("use", "alpha-project"), {"target": tmp_path / "new-root"}),
        (("create",), {"name": "new-project"}),
        (("migrate", "alpha-project"), {"dry_run": False}),
        (("import", "alpha-project"), {}),
        (("repair", "alpha-project"), {}),
        (("rename", "alpha-project"), {}),
        (("update", "alpha-project"), {}),
    )

    for args, kwargs in unsafe_invocations:
        resolution = resolve_invocation(contract, args, kwargs)
        assert resolution.effects == ("artifact_write",)
        assert resolution.pre_task_access == "task_required"
        with pytest.raises(RuntimeError, match="active task profile"):
            enforce_effects("project", resolution.effects, None)


def test_project_selection_with_missing_target_retains_artifact_write(tmp_path):
    target = tmp_path / "external-target"
    target.mkdir()
    project_action(
        tmp_path,
        "create",
        name="External Project",
        target=target,
        output_format="dict",
    )
    target.rmdir()
    contract = build_static_action_contracts(anchor_home=tmp_path)["project"]

    resolution = resolve_invocation(contract, ("use", "external-project"), {})

    assert (resolution.effects, resolution.pre_task_access, resolution.error) == (
        ("artifact_write",),
        "task_required",
        None,
    )
    with pytest.raises(RuntimeError, match="active task profile"):
        enforce_effects("project", resolution.effects, None)


def test_project_migrate_dry_run_is_polymorphic():
    assert resolved("project", "migrate", dry_run=True) == "read"
    assert resolved("project", "migrate", dry_run=False) == "artifact_write"


@pytest.mark.parametrize("keyword", [
    "suppress_category", "unsuppress_category", "suppress_id", "unsuppress_id",
    "file_override", "remove_override",
])
def test_config_mutation_keywords(keyword):
    assert resolved("config", **{keyword: "x"}) == "artifact_write"
    assert resolved("config", harmless=True) == "read"


def test_contract_save_and_apply_sql_modes_do_not_mutate_kwargs():
    kwargs = {"save": True}
    assert resolved("contract", **kwargs) == "artifact_write"
    assert kwargs == {"save": True}
    for mode in ("view", "table"):
        sql_kwargs = {"code_sql": "select 1", "mode": mode}
        assert resolved("apply_sql", **sql_kwargs) == "data_write"
        assert sql_kwargs == {"code_sql": "select 1", "mode": mode}
    assert resolved("apply_sql", code_sql="select 1") == "data_write"


@pytest.mark.parametrize("action,keyword,default,false_effect,true_effect", [
    ("apply_transform", "dry_run", False, "data_write", "read"),
    ("dogfood", "save_as_baseline", False, "read", "artifact_write"),
    ("memory_hygiene", "dry_run", True, "artifact_write", "read"),
])
def test_boolean_contract_tables(action, keyword, default, false_effect, true_effect):
    assert resolved(action) == (true_effect if default else false_effect)
    assert resolved(action, **{keyword: False}) == false_effect
    assert resolved(action, **{keyword: True}) == true_effect
    with pytest.raises(ValueError, match="must be boolean"):
        resolved(action, **{keyword: 1})


def test_resolver_does_not_mutate_original_mapping():
    original = {"mode": "table"}
    resolve_invocation(CONTRACTS["apply_sql"], (), original)
    assert original == {"mode": "table"}


@pytest.mark.parametrize("action,args,kwargs", [
    ("problem", (object(),), {}), ("problem", ("unknown",), {}),
    ("spec", ("unknown",), {}), ("project", ("migrate",), {"dry_run": "yes"}),
    ("contract", (), {"save": "yes"}), ("apply_sql", (), {"mode": "invalid"}),
])
def test_malformed_and_unknown_resolution_uses_all_maxima_then_remains_invalid(action, args, kwargs):
    contract = CONTRACTS[action]
    outcome = resolve_effects(contract, args, kwargs)
    assert outcome.effects == maximal_effects(contract.allowed_effects)
    assert outcome.error is not None
    with pytest.raises((TypeError, ValueError)):
        resolved(action, *args, **kwargs)


def test_incomparable_source_and_data_maxima_are_both_retained():
    contract = ActionContract(
        frozenset({"source_write", "data_write"}),
        frozenset({"task_required"}),
        None,
    )
    assert set(resolve_effects(contract, (), {}).effects) == {"source_write", "data_write"}
    source = normalize_task_profile(execution_mode="source_change")
    with pytest.raises(RuntimeError):
        enforce_effects("dynamic", resolve_effects(contract, (), {}).effects, source)
    dual = normalize_task_profile(
        execution_mode="source_change", traits=["source-change", "data-change"],
    )
    enforce_effects("dynamic", resolve_effects(contract, (), {}).effects, dual)


def test_pre_dispatch_invokes_stateful_effect_resolver_exactly_once():
    from odibi_anchor._dispatcher._pre_dispatch import run_pre_dispatch_enforcement

    calls = []
    def inconsistent(_args, _kwargs):
        calls.append(None)
        return InvocationSemantics(
            "read" if len(calls) == 1 else "source_write",
            "task_required",
        )

    state = SimpleNamespace(
        prior_learn_debt=False, active_task_mode=None, active_task_profile=None,
        skills_loaded=set(), spec_persisted=False, spec_review_rating=None,
        files_at_last_checkpoint=0,
    )
    run_pre_dispatch_enforcement(
        "map", (), {}, session_state=state, session_timings=[],
        session_files_changed=set(), session_boot_manifest={},
        planning_required_actions=frozenset(), root=".",
        action_contract=ActionContract(
            frozenset({"read", "source_write"}),
            frozenset({"task_required"}),
            inconsistent,
        ),
    )
    assert len(calls) == 1


def test_learning_recovery_allows_pending_memory_disposition(monkeypatch):
    from odibi_anchor._dispatcher._pre_dispatch import run_pre_dispatch_enforcement
    from odibi_anchor.codebase import structured_learning_context

    monkeypatch.setattr(
        structured_learning_context,
        "active_learning_obligation",
        lambda **_owner: {"task_window_id": "prior-task"},
    )
    state = SimpleNamespace(
        prior_learn_debt=True, task_window_id="restarted-task", active_task_mode=None,
        active_project="project-a",
        active_task_profile=None,
        skills_loaded=set(), spec_persisted=False, spec_review_rating=None,
        files_at_last_checkpoint=0,
    )
    run_pre_dispatch_enforcement(
        "memory", ("disposition",), {"disposition": "irrelevant", "reason": {"basis": "bounded"}},
        session_state=state, session_timings=[], session_files_changed=set(),
        session_boot_manifest={}, planning_required_actions=frozenset(), root=".",
        action_contract=CONTRACTS["memory"],
    )
    with pytest.raises(RuntimeError, match="learning recovery is active"):
        run_pre_dispatch_enforcement(
            "memory", ("seed",), {"command": "load"}, session_state=state,
            session_timings=[], session_files_changed=set(), session_boot_manifest={},
            planning_required_actions=frozenset(), root=".", action_contract=CONTRACTS["memory"],
        )


@pytest.mark.parametrize("action,args", [
    ("learning", ("capture",)),
    ("memory", ("evaluate",)),
])
def test_current_post_gate_learning_closure_bypasses_fresh_planning_limit(
    monkeypatch, action, args,
):
    from odibi_anchor._dispatcher._pre_dispatch import run_pre_dispatch_enforcement
    from odibi_anchor.codebase import structured_learning_context

    monkeypatch.setattr(
        structured_learning_context,
        "active_learning_obligation",
        lambda **_owner: {"task_window_id": "current-task"},
    )
    state = SimpleNamespace(
        prior_learn_debt=False, task_window_id="current-task", active_task_mode="implementation",
        active_project="project-a",
        active_task_profile=normalize_task_profile(execution_mode="source_change"),
        skills_loaded=set(), spec_persisted=False, spec_review_rating=None,
        files_at_last_checkpoint=0, task_verification_epoch=0,
        pre_task_task_required_attempts=7, checkpoint_in_progress=None,
    )
    decision = run_pre_dispatch_enforcement(
        action, args, {}, session_state=state,
        session_timings=[{"action": "task", "passed": True}, {"action": "gate", "passed": True}],
        session_files_changed=set(), session_boot_manifest={},
        planning_required_actions=frozenset(), root=".", action_contract=CONTRACTS[action],
    )
    assert decision is None  # compatibility-call return; absence of RuntimeError is decisive
    assert state.pre_task_task_required_attempts == 7


@pytest.mark.parametrize("command,effect", [
    ("inspect", "read"),
    ("declare_abandoned", "governance_write"),
    ("recover", "governance_write"),
])
def test_memory_recovery_effects_are_command_specific(command, effect):
    resolution = resolve_invocation(
        CONTRACTS["memory"], ("recovery",), {"action": command},
    )
    assert resolution.error is None
    assert resolution.effects == (effect,)
    assert resolution.pre_task_access == "task_required"


def _run_apply_sql_pre_dispatch(mode_marker, timings, profile, *, has_spec=True):
    from odibi_anchor._dispatcher._pre_dispatch import run_pre_dispatch_enforcement

    kwargs = {} if mode_marker is None else {"mode": mode_marker}
    state = SimpleNamespace(
        prior_learn_debt=False, active_task_mode=None, active_task_profile=profile,
        skills_loaded=set(), spec_persisted=has_spec,
        linked_spec="DATA_SPEC" if has_spec else None,
        persisted_spec_name="DATA_SPEC" if has_spec else None,
        reviewed_spec_name="DATA_SPEC" if has_spec else None,
        spec_review_rating="good" if has_spec else None,
        files_at_last_checkpoint=0,
    )
    return run_pre_dispatch_enforcement(
        "apply_sql", (), kwargs, session_state=state, session_timings=timings,
        session_files_changed=set(), session_boot_manifest={},
        planning_required_actions=frozenset(), root=".",
        action_contract=CONTRACTS["apply_sql"],
    )


@pytest.mark.parametrize("mode", [None, "view", "table"])
def test_pre_dispatch_apply_sql_valid_modes_require_quality(mode):
    profile = normalize_task_profile(execution_mode="data_change")
    with pytest.raises(RuntimeError, match="quality check"):
        _run_apply_sql_pre_dispatch(mode, [], profile)
    _run_apply_sql_pre_dispatch(mode, [{"action": "quality", "error": None}], profile)


def test_pre_dispatch_apply_sql_invalid_mode_reports_resolution_error():
    profile = normalize_task_profile(execution_mode="data_change")
    with pytest.raises(RuntimeError, match=r"invocation semantics could not be resolved.*mode must be"):
        _run_apply_sql_pre_dispatch("invalid", [], profile)


def test_error_shaped_task_required_result_keeps_its_error_without_advisory():
    from odibi_anchor._dispatcher._post_dispatch import run_post_dispatch

    result = {"kind": "review_context", "error": "handler failed", "risks": []}
    resolution = resolve_invocation(CONTRACTS["review"], (), {})
    decision = SimpleNamespace(
        pre_task=True, pre_task_access="task_required", attempt=3,
    )
    state = SimpleNamespace(
        active_problem=None, active_task_profile=None, prior_learn_debt=False,
        skill_hints_emitted=set(), skills_loaded=set(), observed_effects=[],
    )
    actual = run_post_dispatch(
        "review", result, None, (), {},
        session_timings=[], session_files_changed=set(), session_state=state,
        planning_required_actions=frozenset(), invocation_resolution=resolution,
        pre_task_decision=decision,
    )

    assert actual == result
    assert actual["risks"] == []


def test_successful_authority_write_checkpoints_configured_active_state(tmp_path, monkeypatch):
    import sqlite3

    import odibi_anchor._dispatcher._boot as boot
    from odibi_anchor._dispatcher._post_dispatch import run_post_dispatch

    database = tmp_path / "memory.db"
    sqlite3.connect(database).close()
    durable = tmp_path / "durable"
    anchor_home = tmp_path / "anchor-home"
    durable.mkdir()
    (anchor_home / "workspace" / "projects").mkdir(parents=True)
    monkeypatch.setitem(boot._ENV, "memory_db", str(database))
    monkeypatch.setitem(boot._ENV, "durable_root", str(durable))
    monkeypatch.setitem(
        boot._ENV, "runtime_paths", SimpleNamespace(anchor_home=anchor_home)
    )
    monkeypatch.setitem(boot._ENV, "authority_id", "work")
    monkeypatch.setitem(boot._ENV, "trust_domain", "work")
    monkeypatch.setitem(boot._ENV, "is_databricks", False)
    state = SimpleNamespace(
        active_problem=None, active_task_profile=None, prior_learn_debt=False,
        skill_hints_emitted=set(), skills_loaded=set(), observed_effects=[],
    )

    result = run_post_dispatch(
        "log", {"kind": "log", "write_performed": True}, None, (), {},
        session_timings=[], session_files_changed=set(), session_state=state,
        planning_required_actions=frozenset(),
        invocation_resolution=resolve_invocation(CONTRACTS["log"], (), {}),
    )

    assert result["durable_state"]["action"] == "created"
    assert list((durable / "work" / "snapshots").glob("*.manifest.json"))


def test_successful_memory_rejection_is_preserved_in_durable_restore(tmp_path, monkeypatch):
    import sqlite3

    import odibi_anchor._dispatcher._boot as boot
    from odibi_anchor._dispatcher._post_dispatch import run_post_dispatch
    from odibi_anchor.codebase._memory_db import (
        close_db,
        insert_memory,
        reject_memory_entry,
    )
    from odibi_anchor.durability import restore_latest

    database = tmp_path / "memory.db"
    durable = tmp_path / "durable"
    restored = tmp_path / "restored.db"
    anchor_home = tmp_path / "anchor-home"
    restored_artifacts = tmp_path / "restored-projects"
    durable.mkdir()
    (anchor_home / "workspace" / "projects").mkdir(parents=True)
    memory = insert_memory(
        str(database), project="alpha", type="gotcha", content="Rejected candidate"
    )
    rejection = reject_memory_entry(str(database), entry_id=memory["id"])
    monkeypatch.setitem(boot._ENV, "memory_db", str(database))
    monkeypatch.setitem(boot._ENV, "durable_root", str(durable))
    monkeypatch.setitem(
        boot._ENV, "runtime_paths", SimpleNamespace(anchor_home=anchor_home)
    )
    monkeypatch.setitem(boot._ENV, "authority_id", "work")
    monkeypatch.setitem(boot._ENV, "trust_domain", "work")
    monkeypatch.setitem(boot._ENV, "is_databricks", False)
    state = SimpleNamespace(
        active_problem=None, active_task_profile=None, prior_learn_debt=False,
        skill_hints_emitted=set(), skills_loaded=set(), observed_effects=[],
    )

    result = run_post_dispatch(
        "reject", rejection, None, (), {},
        session_timings=[], session_files_changed=set(), session_state=state,
        planning_required_actions=frozenset(),
        invocation_resolution=resolve_invocation(CONTRACTS["reject"], (), {}),
    )
    close_db(str(database))
    restore_latest(
        durable_root=durable,
        destination_db=restored,
        destination_artifacts=restored_artifacts,
        authority_id="work",
    )

    assert result["durable_state"]["action"] == "created"
    with sqlite3.connect(restored) as connection:
        status = connection.execute(
            "SELECT status FROM memories WHERE id=?", (memory["id"],)
        ).fetchone()[0]
    assert status == "rejected"


def test_pre_dispatch_data_change_requires_exact_spec_evidence():
    profile = normalize_task_profile(execution_mode="data_change")
    with pytest.raises(RuntimeError, match="requires a specification"):
        _run_apply_sql_pre_dispatch(
            "table", [{"action": "quality", "error": None}], profile,
            has_spec=False,
        )


def test_pre_dispatch_strictest_wins_when_effect_allows_but_legacy_blocks():
    profile = normalize_task_profile(execution_mode="data_change")
    with pytest.raises(RuntimeError, match="quality check"):
        _run_apply_sql_pre_dispatch("table", [], profile)


def test_pre_dispatch_strictest_wins_when_legacy_allows_but_effect_blocks():
    profile = normalize_task_profile(execution_mode="read_only")
    with pytest.raises(RuntimeError, match="data_write is incompatible"):
        _run_apply_sql_pre_dispatch(
            "table", [{"action": "quality", "error": None}], profile,
        )


def test_overlap_effect_gate_blocks_without_weakening_legacy_layer():
    read_only = normalize_task_profile(execution_mode="read_only")
    with pytest.raises(RuntimeError):
        enforce_effects("safe", ("source_write",), read_only)
    with pytest.raises(RuntimeError):
        enforce_effects("apply_sql", ("data_write",), read_only)
    with pytest.raises(RuntimeError):
        enforce_effects("sync", ("external_mutation",), read_only)
    enforce_effects("task", ("artifact_write",), None)
    enforce_effects("new_session", ("artifact_write",), None)
    for action in ("checkpoint", "learn", "skill_loaded", "log"):
        with pytest.raises(RuntimeError, match="active task profile"):
            enforce_effects(action, ("artifact_write",), None)
        enforce_effects(action, ("artifact_write",), read_only)
    for action in ("memory", "learning"):
        with pytest.raises(RuntimeError, match="active task profile"):
            enforce_effects(action, ("governance_write",), None)
        enforce_effects(action, ("governance_write",), read_only)
    for action in ("save", "archive", "import_md", "db_migrate", "memory_hygiene", "register_tool"):
        with pytest.raises(RuntimeError):
            enforce_effects(action, ("artifact_write",), read_only)
