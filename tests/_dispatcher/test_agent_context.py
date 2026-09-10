"""Contract tests for the bounded read-only agent context envelope."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from odibi_anchor._dispatcher._agent_context import build_agent_context, render_agent_context
from odibi_anchor._dispatcher._operating_protocol import build_operating_protocol
from odibi_anchor._dispatcher._project import RouteBinding
from odibi_anchor._dispatcher._runtime_capabilities import collect_runtime_capabilities
from odibi_anchor._repository_snapshot import TaskRepositoryBaseline
from odibi_anchor.planning._task_profile import normalize_task_profile


def _state(profile=None, **overrides):
    values = dict(
        active_task_profile=profile,
        active_project="project-a",
        artifact_root="/artifacts/a",
        target_root="/work/a",
        session_id="session-a",
        task_window_id="ltw-a",
        linked_problem="PRB-2026-0001",
        linked_spec="SPEC_A",
        linked_work_item="WI-2026-0001",
        task_goal="Implement bounded context",
        routing_stale=False,
        learning_obligation_id=None,
        latest_closed_obligation_id=None,
        prior_learn_debt=False,
        task_verification_epoch=0,
        terminal_status=None,
        session_name="campaign",
        skills_loaded=set(),
        task_repository_baseline=None,
        evidence_ledger=[],
        managed_artifact_ledger=[],
        continuity_status="bound",
        runtime_current_revision="a" * 40,
        runtime_current_fingerprint="sha256:" + "b" * 64,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def _binding() -> RouteBinding:
    return RouteBinding(
        project_id="project-a",
        target_root="/work/a",
        artifact_root="/artifacts/a",
        anchor_home="/anchor",
        binding_source="explicit",
        runtime_instance_id="runtime-a",
    )


def _context(state, *, view="compact", binding=None, timings=()):
    protocol = build_operating_protocol(
        state,
        action="context",
        session_timings=timings,
    )
    return build_agent_context(
        state,
        protocol=protocol,
        route_binding=binding,
        view=view,
    )


def test_compact_context_is_deterministic_json_safe_and_route_bound():
    state = _state(normalize_task_profile(execution_mode="source_change"))
    first = _context(state, binding=_binding())
    second = _context(state, binding=_binding())

    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    assert tuple(first["facts"]) == ("project", "task", "lifecycle")
    assert first["facts"]["project"]["status"] == "verified"
    assert first["facts"]["project"]["value"]["project_id"] == "project-a"
    assert first["facts"]["project"]["provenance"] == {"source": "route_binding:v1"}
    assert first["facts"]["task"]["value"]["task_window_id"] == "ltw-a"
    assert first["omitted_sections"] == [
        "repository",
        "evidence",
        "artifacts",
        "capabilities",
        "policy",
    ]
    assert first["next_operation"]["copy_ready"] == 'anchor("preflight")'


def test_full_context_projects_safe_baseline_and_ledger_metadata_only():
    baseline = TaskRepositoryBaseline(
        target_worktree="/work/a",
        branch="campaign/a",
        configured_target_ref="origin/main",
        target_sha="1" * 40,
        merge_base_sha="2" * 40,
        task_start_head_sha="3" * 40,
        captured_at="2026-09-10T12:00:00+00:00",
    )
    state = _state(
        normalize_task_profile(execution_mode="source_change"),
        task_repository_baseline=baseline,
        evidence_ledger=[{"kind": "test", "secret": "must-not-leak"}],
        managed_artifact_ledger=[{"kind": "spec", "raw": "must-not-leak"}],
    )

    result = _context(state, view="full", binding=_binding())

    assert result["omitted_sections"] == []
    assert result["facts"]["repository"]["status"] == "verified"
    assert result["facts"]["repository"]["value"]["task_start_head_sha"] == "3" * 40
    assert result["facts"]["evidence"]["value"] == {"count": 1, "kinds": ["test"]}
    assert result["facts"]["artifacts"]["value"] == {"count": 1, "kinds": ["spec"]}
    assert result["summary"] == "full derived context for phase execution"
    assert "must-not-leak" not in json.dumps(result)


def test_full_context_normalizes_runtime_capabilities_without_mutation():
    state = _state()
    before = vars(state).copy()

    result = _context(state, view="full", binding=_binding())

    capabilities = result["facts"]["capabilities"]["value"]
    assert capabilities["python"]["state"] == "available"
    assert capabilities["odibi_anchor_package"]["state"] == "available"
    assert capabilities["local_git_repository"]["state"] == "incompatible"
    assert capabilities["automatic_repair"]["state"] == "not_applicable"
    assert vars(state) == before


def test_ledger_metadata_is_bounded_without_hiding_total_count():
    state = _state(
        evidence_ledger=[{"kind": f"kind-{index:02d}"} for index in range(25)],
    )

    result = _context(state, view="summary", binding=_binding())

    evidence = result["facts"]["evidence"]["value"]
    assert evidence["count"] == 25
    assert evidence["kinds"] == [f"kind-{index:02d}" for index in range(20)]


def test_mutable_active_project_is_never_presented_as_routing_authority():
    state = _state()

    result = _context(state)

    project = result["facts"]["project"]
    assert project["status"] == "unverified"
    assert project["value"] == {"project_id": "project-a"}
    assert "not routing authority" in project["reason"]


def test_route_and_session_project_conflict_fails_closed():
    state = _state(active_project="project-b")

    result = _context(state, binding=_binding())

    assert result["status"] == "conflicting"
    assert result["facts"]["project"]["status"] == "conflicting"
    assert result["facts"]["project"]["value"]["project_id"] == "project-a"


def test_stale_route_is_not_presented_as_current_verified_authority():
    state = _state(routing_stale=True)

    result = _context(state, binding=_binding())

    assert result["status"] == "stale"
    assert result["facts"]["project"]["status"] == "stale"
    assert result["facts"]["project"]["reason"] == "the bound route changed and requires rebootstrap"


def test_views_validate_and_full_markdown_discloses_provenance():
    state = _state()
    protocol = build_operating_protocol(state, action="context")

    with pytest.raises(ValueError, match="view must"):
        build_agent_context(state, protocol=protocol, view="everything")
    with pytest.raises(ValueError, match="output_format must"):
        build_agent_context(state, protocol=protocol, output_format="yaml")

    rendered = build_agent_context(
        state,
        protocol=protocol,
        route_binding=_binding(),
        output_format="markdown",
    )
    assert "# Agent Context" in rendered
    assert "Source: `route_binding:v1`" in rendered
    assert "## Next operation" in rendered

    concise = render_agent_context(_context(state, binding=_binding()), concise=True)
    assert concise.count("## Agent Context v1") == 1
    assert '**Next:** `anchor("prepare", operation=\'task.create\', inputs={})`' in concise
    assert "references/odibi-anchor/workflow.md" in concise


def test_required_skill_is_the_single_copy_ready_next_operation():
    profile = normalize_task_profile(execution_mode="source_change")
    state = _state(profile, skills_loaded=set())
    protocol = build_operating_protocol(state, action="context")
    protocol["required_now"] = [
        {
            "id": "load_required_skill",
            "satisfy_with": {"route": "skill_loaded", "skill": "writing-tests"},
        }
    ]

    result = build_agent_context(state, protocol=protocol, route_binding=_binding())

    assert result["blocker"] == "load_required_skill"
    assert result["next_operation"] == {
        "status": "ready",
        "kind": "dispatcher_call",
        "action": "skill_loaded",
        "args": ["writing-tests"],
        "kwargs": {},
        "copy_ready": 'anchor("skill_loaded", "writing-tests")',
        "reason": "required lifecycle obligation: load_required_skill",
    }
    assert result["suggested_next_actions"] == ['anchor("skill_loaded", "writing-tests")']


def test_dotted_protocol_route_becomes_dispatcher_action_and_selector():
    state = _state(normalize_task_profile(execution_mode="source_change"))
    protocol = build_operating_protocol(state, action="gate")
    protocol["required_now"] = [
        {
            "id": "learning_closure",
            "satisfy_with": {"route": "learning.assess"},
        }
    ]

    result = build_agent_context(state, protocol=protocol, route_binding=_binding())

    assert result["next_operation"]["kind"] == "semantic_input_preparation"
    assert result["next_operation"]["action"] == "prepare"
    assert result["next_operation"]["kwargs"] == {
        "operation": "learning.assess",
        "inputs": {},
    }


def test_session_rebootstrap_and_terminal_operations_are_never_invalid_cw_calls():
    state = _state(active_task_profile=None, session_name=None)
    start = _context(
        state,
        binding=_binding(),
        timings=(
            {"action": "status", "error": None, "passed": True},
            {"action": "audit_history", "error": None, "passed": True},
        ),
    )["next_operation"]
    assert start["copy_ready"] == (
        'anchor("new_session", name=\'odibi_anchor_session\', inline=True)'
    )

    stale = _state(routing_stale=True)
    bootstrap = _context(stale, binding=_binding())["next_operation"]
    assert bootstrap["kind"] == "runtime_bootstrap"
    assert bootstrap["action"] == "rebootstrap_runtime"
    assert not bootstrap["copy_ready"].startswith("anchor(")

    terminal = _state(
        normalize_task_profile(execution_mode="source_change"),
        terminal_status="blocked",
    )
    stopped = _context(terminal, binding=_binding())["next_operation"]
    assert stopped["kind"] == "terminal"
    assert stopped["status"] == "not_applicable"


def test_repository_capabilities_distinguish_missing_nonrepo_unborn_and_shallow(
    tmp_path, monkeypatch,
):
    state = _state()
    route = SimpleNamespace(target_root=str(tmp_path))
    monkeypatch.setattr("odibi_anchor._dispatcher._runtime_capabilities.shutil.which", lambda _: None)
    missing = collect_runtime_capabilities(state, route)
    assert missing["local_git_repository"]["state"] == "missing"
    assert missing["repository_surface"]["state"] == "missing"
    assert missing["repository_history"]["state"] == "missing"

    monkeypatch.undo()
    nonrepo = collect_runtime_capabilities(state, route)
    assert nonrepo["local_git_repository"]["state"] == "incompatible"
    assert nonrepo["repository_surface"]["state"] == "incompatible"
    assert nonrepo["repository_history"]["state"] == "unavailable"

    unborn_root = tmp_path / "unborn"
    unborn_root.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=unborn_root, check=True, capture_output=True)
    unborn = collect_runtime_capabilities(state, SimpleNamespace(target_root=str(unborn_root)))
    assert unborn["local_git_repository"]["state"] == "available"
    assert unborn["repository_surface"]["state"] == "available"
    assert unborn["repository_history"]["state"] == "unavailable"

    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=source, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Tests"], cwd=source, check=True)
    subprocess.run(["git", "config", "user.email", "tests@example.invalid"], cwd=source, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=source, check=True)
    Path(source, "value.txt").write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=source, check=True)
    subprocess.run(["git", "commit", "-m", "one"], cwd=source, check=True, capture_output=True)
    Path(source, "value.txt").write_text("two\n", encoding="utf-8")
    subprocess.run(["git", "commit", "-am", "two"], cwd=source, check=True, capture_output=True)
    shallow_root = tmp_path / "shallow"
    subprocess.run(
        ["git", "clone", "--depth=1", source.as_uri(), str(shallow_root)],
        check=True,
        capture_output=True,
    )
    shallow = collect_runtime_capabilities(
        state, SimpleNamespace(target_root=str(shallow_root)),
    )
    assert shallow["local_git_repository"]["state"] == "available"
    assert shallow["repository_history"]["state"] == "incompatible"
    assert shallow["repository_history"]["value"] == {"complete": False}
