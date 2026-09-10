"""Canonical authority-derived handoff behavior and stale-state coverage."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from odibi_anchor._dispatcher._action_preparation import prepare_action
from odibi_anchor._dispatcher._baseline_qualification import qualify_task_baseline
from odibi_anchor._dispatcher._canonical_handoff import (
    canonical_handoff,
    validate_canonical_handoff,
)
from odibi_anchor._dispatcher._project import RouteBinding
from odibi_anchor.planning._task_profile import normalize_task_profile


def _git(root, *args):
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True,
    ).stdout.strip()


def _authority(tmp_path):
    repository = tmp_path / "repo"
    repository.mkdir()
    _git(repository, "init", "-b", "main")
    _git(repository, "config", "user.email", "tests@example.invalid")
    _git(repository, "config", "user.name", "Tests")
    _git(repository, "config", "commit.gpgsign", "false")
    (repository / "source.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "baseline")
    artifacts = tmp_path / "artifacts"
    for name in ("problems", "specs", "work_items", "decisions", "notebooks"):
        (artifacts / name).mkdir(parents=True, exist_ok=True)
    from odibi_anchor._repository_snapshot import capture_task_repository_baseline

    baseline = capture_task_repository_baseline(repository, "main")
    profile = normalize_task_profile(
        work_type="change", execution_mode="source_change", risk="high", rigor="full",
        domains=["code", "data"], traits=["implementation", "migration"],
        caller_required_evidence=[{
            "id": "evidence-1", "kind": "pytest", "description": "Run focused tests",
            "required_before": "gate",
        }],
    )
    state = SimpleNamespace(
        artifact_root=str(artifacts), target_root=str(repository), active_project="project-a",
        task_window_id="ltw_exact", linked_problem=None, linked_spec=None,
        linked_work_item=None, task_goal="Implement safe parser", active_task_profile=profile,
        task_repository_baseline=baseline, task_repository_write_fingerprints={},
        task_repository_baseline_qualification=qualify_task_baseline(
            baseline, task_window_id="ltw_exact", execution_mode="source_change",
            target_root=str(repository),
        ),
        task_handoff_context={
            "intent": {"task": "Implement parser", "goal": "Reject malformed input"},
            "background": {"summary": "Malformed input causes operator rework"},
            "scope": {"in_scope": ["parser"], "out_of_scope": ["transport"]},
            "constraints": ["No dependency installation"],
            "verification": {
                "acceptance_criteria": ["Malformed input is rejected"],
                "stop_conditions": ["Repository authority drifts"],
            },
            "risks": ["Compatibility regression"],
        },
        evidence_ledger=[], managed_artifact_ledger=[], repository_provider=None,
        runtime_instance_id="mcp:test", runtime_current_revision=_git(repository, "rev-parse", "HEAD"),
        runtime_current_fingerprint="sha256:test", continuity_status="ready",
    )
    binding = RouteBinding(
        "project-a", str(repository), str(artifacts), str(tmp_path / "anchor"),
        "explicit", "mcp:test",
    )
    return state, binding


def test_canonical_handoff_is_self_contained_content_addressed_and_persistent(tmp_path):
    state, binding = _authority(tmp_path)
    result = canonical_handoff(state, binding)

    assert result["kind"] == "handoff_context"
    assert result["schema_version"] == "2.0"
    assert result["business_reason"] == "Malformed input causes operator rework"
    assert result["scope"]["out_of_scope"] == ["transport"]
    assert result["definition_of_done"] == ["Malformed input is rejected"]
    assert result["source"]["qualification"]["status"] == "attested"
    assert result["first_action"]["action"] == "task"
    assert result["execution_recommendation"] == {"thread": "fresh", "environment": "same"}
    artifact = Path(result["artifact_path"])
    artifact_bytes = artifact.read_bytes()
    payload = json.loads(artifact_bytes)
    assert payload["authority_sha256"] == result["authority_sha256"]
    assert result["content_sha256"] == "sha256:" + hashlib.sha256(artifact_bytes).hexdigest()


def test_missing_business_reason_remains_explicit_not_fabricated(tmp_path):
    state, binding = _authority(tmp_path)
    state.task_handoff_context["background"] = {"summary": None}

    result = canonical_handoff(state, binding, persist=False)

    assert result["business_reason"] is None
    assert result["missing_semantics"][0]["field"] == "business_reason"
    assert result["write_performed"] is False


def test_repeated_generation_is_deterministic_and_first_action_is_copy_ready(tmp_path):
    state, binding = _authority(tmp_path)
    first = canonical_handoff(state, binding, persist=False)
    second = canonical_handoff(state, binding, persist=False)

    assert first == second
    namespace = {"anchor": lambda action, *args, **kwargs: (action, args, kwargs)}
    action, args, kwargs = eval(first["first_action"]["copy_ready"], namespace)
    assert action == "task"
    assert args == ("Implement parser",)
    assert kwargs["acceptance_criteria"] == ["Malformed input is rejected"]
    assert kwargs["domains"] == ["code", "data"]
    assert kwargs["traits"] == ["implementation", "migration"]
    assert kwargs["caller_required_evidence"][0]["id"] == "evidence-1"
    assert kwargs["execution_mode"] == "source_change"

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json,sys; p=json.load(open(sys.argv[1])); "
                "anchor=lambda action,*args,**kwargs: [action,args,kwargs]; "
                "print(json.dumps(eval(p['first_action']['copy_ready'])))"
            ),
            canonical_handoff(state, binding)["artifact_path"],
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    fresh_action, fresh_args, fresh_kwargs = json.loads(completed.stdout)
    assert fresh_action == "task"
    assert fresh_args == ["Implement parser"]
    assert fresh_kwargs["goal"] == "Reject malformed input"
    assert fresh_kwargs["acceptance_criteria"] == ["Malformed input is rejected"]


def test_validation_fails_closed_after_repository_authority_drift(tmp_path):
    state, binding = _authority(tmp_path)
    packet = canonical_handoff(state, binding, persist=False)
    Path(binding.target_root, "source.py").write_text("VALUE = 2\n", encoding="utf-8")

    result = validate_canonical_handoff(packet, state, binding)

    assert result["status"] == "stale"
    assert result["conflicts"] == ["first_action", "repository_state_sha256"]
    assert result["first_action"] is None


def test_validation_detects_dirty_content_change_with_same_changed_path(tmp_path):
    state, binding = _authority(tmp_path)
    source = Path(binding.target_root, "source.py")
    source.write_text("VALUE = 2\n", encoding="utf-8")
    packet = canonical_handoff(state, binding, persist=False)
    source.write_text("VALUE = 3\n", encoding="utf-8")

    result = validate_canonical_handoff(packet, state, binding)

    assert result["status"] == "stale"
    assert result["conflicts"] == ["repository_state_sha256"]


def test_validation_detects_staged_content_change_with_restored_worktree(tmp_path):
    state, binding = _authority(tmp_path)
    source = Path(binding.target_root, "source.py")
    source.write_text("VALUE = 2\n", encoding="utf-8")
    _git(Path(binding.target_root), "add", "source.py")
    packet = canonical_handoff(state, binding, persist=False)

    source.write_text("VALUE = 3\n", encoding="utf-8")
    _git(Path(binding.target_root), "add", "source.py")
    source.write_text("VALUE = 2\n", encoding="utf-8")

    result = validate_canonical_handoff(packet, state, binding)

    assert result["status"] == "stale"
    assert result["conflicts"] == ["repository_state_sha256"]


def test_validation_detects_unborn_staged_content_change_with_restored_worktree(tmp_path):
    state, binding = _authority(tmp_path)
    repository = tmp_path / "unborn"
    repository.mkdir()
    _git(repository, "init", "-b", "main")
    from odibi_anchor._repository_snapshot import capture_task_repository_baseline

    baseline = capture_task_repository_baseline(repository, "main")
    source = repository / "source.py"
    source.write_text("VALUE = 2\n", encoding="utf-8")
    _git(repository, "add", "source.py")
    state.target_root = str(repository)
    state.task_repository_baseline = baseline
    state.task_repository_baseline_qualification = qualify_task_baseline(
        baseline, task_window_id=state.task_window_id, execution_mode="source_change",
        target_root=str(repository),
    )
    binding = RouteBinding(
        binding.project_id, str(repository), binding.artifact_root, binding.anchor_home,
        binding.binding_source, binding.runtime_instance_id,
    )
    packet = canonical_handoff(state, binding, persist=False)

    source.write_text("VALUE = 3\n", encoding="utf-8")
    _git(repository, "add", "source.py")
    source.write_text("VALUE = 2\n", encoding="utf-8")

    result = validate_canonical_handoff(packet, state, binding)

    assert result["status"] == "stale"
    assert result["conflicts"] == ["repository_state_sha256"]


def test_validation_detects_exact_spec_body_and_first_action_changes(tmp_path):
    state, binding = _authority(tmp_path)
    spec = Path(binding.artifact_root, "specs", "PARSER_SPEC.md")
    spec.write_text("---\nstatus: draft\n---\n# Parser\nOriginal detail.\n", encoding="utf-8")
    state.linked_spec = "PARSER"
    packet = canonical_handoff(state, binding, persist=False)
    spec.write_text("---\nstatus: draft\n---\n# Parser\nDifferent detail.\n", encoding="utf-8")
    state.task_handoff_context["intent"]["task"] = "Implement a different parser boundary"

    result = validate_canonical_handoff(packet, state, binding)

    assert result["status"] == "stale"
    assert result["conflicts"] == ["first_action", "managed_record_sha256"]


def test_supported_handoff_fields_are_preserved_and_unknown_fields_fail(tmp_path):
    state, binding = _authority(tmp_path)
    result = canonical_handoff(
        state,
        binding,
        persist=False,
        subject="Parser campaign",
        blockers=["Await compatibility decision"],
        evidence_chain=[{"tool": "pytest", "summary": "Focused tests passed"}],
        artifacts=[{"path": "parser.py", "role": "source"}],
        context_needed=["PARSER_SPEC.md"],
        skip=["Do not repeat baseline capture"],
    )

    assert result["subject"] == "Parser campaign"
    assert result["blockers"] == ["Await compatibility decision"]
    assert result["metrics"]["is_blocked"] is True
    assert result["evidence_chain"][0]["tool"] == "pytest"
    assert result["artifacts"][0]["path"] == "parser.py"
    assert result["continuation"]["context_needed"] == ["PARSER_SPEC.md"]
    assert result["continuation"]["skip"] == ["Do not repeat baseline capture"]
    with pytest.raises(ValueError, match="unsupported canonical handoff fields: invented"):
        canonical_handoff(state, binding, persist=False, invented="value")


def test_invalid_format_has_no_write_and_symlinked_handoff_directory_is_rejected(tmp_path):
    state, binding = _authority(tmp_path)
    handoffs = Path(binding.artifact_root, "notebooks", "handoffs")
    with pytest.raises(ValueError, match="output_format"):
        canonical_handoff(state, binding, output_format="yaml")
    assert not handoffs.exists()

    outside = tmp_path / "outside"
    outside.mkdir()
    handoffs.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="link or reparse point"):
        canonical_handoff(state, binding)
    assert list(outside.iterdir()) == []


def test_databricks_handoff_projects_preimage_metadata_without_content(monkeypatch, tmp_path):
    state, binding = _authority(tmp_path)
    from odibi_anchor._repository_snapshot import (
        DatabricksGitFolderIdentity,
        DatabricksGitFolderTaskBaseline,
        DatabricksTaskChangeScope,
        ScopedFilePreimage,
    )

    sentinel = b"SENTINEL-SECRET-BYTES"
    identity = DatabricksGitFolderIdentity(
        "repo-1", "/Workspace/repo", "feature", "a" * 40,
        "https://example.invalid/repo", "github",
    )
    baseline = DatabricksGitFolderTaskBaseline(
        str(Path(binding.target_root)), ("",), (), identity,
        (ScopedFilePreimage("source.py", hashlib.sha256(sentinel).hexdigest(), len(sentinel), sentinel),),
        "2026-09-10T00:00:00+00:00", SimpleNamespace(capture_identity=lambda _: identity),
    )
    scope = DatabricksTaskChangeScope(
        baseline.target_worktree, identity, (), (), "2026-09-10T00:01:00+00:00",
        {"content_changes": {}},
    )
    monkeypatch.setattr(
        "odibi_anchor._repository_snapshot.capture_task_change_scope",
        lambda *_: scope,
    )
    state.task_repository_baseline = baseline
    state.task_repository_baseline_qualification = None
    packet = canonical_handoff(state, binding, persist=False)
    encoded = json.dumps(packet)

    assert sentinel.decode() not in encoded
    assert "content" not in packet["source"]["baseline"]["preimages"][0]
    assert packet["source"]["baseline"]["preimages"][0]["sha256"] == hashlib.sha256(sentinel).hexdigest()
    assert packet["first_action"]["kwargs"]["repository_scope"] == ["."]
    assert packet["first_action"]["kwargs"]["accept_unknown_git_state"] is True


def test_databricks_planning_handoff_prepares_missing_source_authority(tmp_path):
    state, binding = _authority(tmp_path)
    state.active_task_profile = normalize_task_profile(legacy_mode="planning")
    state.task_repository_baseline = None
    state.task_repository_baseline_qualification = None
    state.repository_provider = SimpleNamespace(provider_id="databricks:test")

    packet = canonical_handoff(state, binding, persist=False)
    prepared = prepare_action(
        state,
        route_binding=binding,
        **packet["first_action"]["kwargs"],
    )

    assert packet["first_action"]["action"] == "prepare"
    assert prepared["status"] == "blocked"
    assert [item["field"] for item in prepared["required_missing"]] == [
        "repository_scope", "accept_unknown_git_state",
    ]


def test_validation_fails_closed_for_different_route(tmp_path):
    state, binding = _authority(tmp_path)
    packet = canonical_handoff(state, binding, persist=False)
    foreign = RouteBinding(
        "project-b", binding.target_root, binding.artifact_root, binding.anchor_home,
        "explicit", "mcp:other",
    )
    state.active_project = "project-b"

    result = validate_canonical_handoff(packet, state, foreign)

    assert result["status"] == "stale"
    assert "project_id" in result["conflicts"]
    assert "route_fingerprint" in result["conflicts"]


def test_validation_accepts_same_durable_route_from_fresh_runtime(tmp_path):
    state, binding = _authority(tmp_path)
    packet = canonical_handoff(state, binding, persist=False)
    rebound = RouteBinding(
        binding.project_id, binding.target_root, binding.artifact_root, binding.anchor_home,
        "explicit", "mcp:fresh-process",
    )

    result = validate_canonical_handoff(packet, state, rebound)

    assert result["status"] == "verified"
    assert result["conflicts"] == []


def test_dispatcher_generates_canonical_handoff_and_restores_task_context(tmp_path, monkeypatch):
    state_root = tmp_path / "state"
    repository = tmp_path / "repo"
    repository.mkdir()
    _git(repository, "init", "-b", "main")
    _git(repository, "config", "user.email", "tests@example.invalid")
    _git(repository, "config", "user.name", "Tests")
    _git(repository, "config", "commit.gpgsign", "false")
    (repository / "source.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "baseline")
    state_root.mkdir()
    monkeypatch.setenv("ANCHOR_HOME", str(state_root))
    monkeypatch.setenv("ANCHOR_MEMORY_DB", str(state_root / "memory.db"))
    from odibi_anchor._dispatcher._project import project_action, resolve_route_binding
    from odibi_anchor.bootstrap import init

    project_action(
        state_root, "create", name="handoff-project", target=repository,
        output_format="dict",
    )
    binding = resolve_route_binding(
        state_root, project="handoff-project", runtime_instance_id="test:handoff",
    )
    anchor, _, _ = init(route_binding=binding, output_format="dict")
    anchor("orient", output_format="dict")
    anchor("status", output_format="dict")
    anchor("audit_history", output_format="dict")
    anchor("new_session", name="canonical-handoff", inline=True, output_format="dict")
    problems = Path(binding.artifact_root, "problems")
    existing_problems = set(problems.glob("*.md"))
    with pytest.raises(ValueError, match="retained evidence_ref"):
        anchor(
            "task", "Reject invalid qualification without side effects",
            goal="Leave no Problem artifact for rejected task authority",
            background="A failed qualification must be transactional.",
            mode="implementation", work_type="change", execution_mode="source_change",
            risk="low", rigor="direct", create_problem=True,
            in_scope=["source.py"], constraints=["Do not retain rejected task artifacts."],
            acceptance_criteria=["No Problem is created."], deliverables=["Atomic rejection."],
            baseline_qualification={"outcome": "smoke_passed", "reason": "claimed pass"},
            output_format="dict",
        )
    assert set(problems.glob("*.md")) == existing_problems
    accepted = anchor(
        "task", "Implement parser", goal="Reject malformed input",
        background="Malformed input causes operator rework",
        mode="implementation", work_type="change", execution_mode="source_change",
        risk="low", rigor="direct",
        in_scope=["parser"], out_of_scope=["transport"],
        acceptance_criteria=["Malformed input is rejected"], output_format="dict",
    )
    handoff = anchor("snapshot", mode="handoff", output_format="dict")

    assert handoff["schema_version"] == "2.0"
    assert handoff["authority"]["task_window_id"] == (
        accepted["accepted_task_authority"]["task_window_id"]
    )
    assert handoff["scope"]["out_of_scope"] == ["transport"]
    markdown = anchor("snapshot", mode="handoff", output_format="markdown")
    from odibi_anchor._utils._session_state import _SESSION_STATE

    assert "## Canonical authority" in markdown
    assert any(
        item.kind == "snapshot" and item.path.endswith(".json")
        for item in _SESSION_STATE.managed_artifact_ledger
    )
    next_task = eval(handoff["first_action"]["copy_ready"], {"anchor": anchor})
    assert next_task["task_profile"]["execution_mode"] == "source_change"
    assert next_task["task_profile"]["work_type"] == "change"
    Path(binding.target_root, "source.py").write_text("VALUE = 2\n", encoding="utf-8")
    dirty_handoff = anchor("snapshot", mode="handoff", persist=False, output_format="dict")
    assert dirty_handoff["first_action"]["action"] == "bootstrap_rebind"
    fresh_namespace = {}
    exec(dirty_handoff["first_action"]["copy_ready"], fresh_namespace)
    rebound = fresh_namespace["anchor"]
    restored = rebound("snapshot", mode="handoff", persist=False, output_format="dict")
    assert rebound._task_rebind_result["status"] == "rebound"
    assert restored["business_reason"] == "Malformed input causes operator rework"
    assert restored["acceptance_criteria"] == ["Malformed input is rejected"]


def test_unbound_legacy_handoff_markdown_remains_supported(tmp_path, monkeypatch):
    state_root = tmp_path / "state"
    repository = tmp_path / "repo"
    repository.mkdir()
    monkeypatch.setenv("ANCHOR_HOME", str(state_root))
    monkeypatch.setenv("ANCHOR_MEMORY_DB", str(state_root / "memory.db"))
    monkeypatch.delenv("ANCHOR_PROJECT_ID", raising=False)
    from odibi_anchor.bootstrap import init

    anchor, _, _ = init(root=str(repository), output_format="dict")
    anchor("status", output_format="dict")
    anchor("audit_history", output_format="dict")
    anchor("new_session", name="legacy-handoff", inline=True, output_format="dict")
    anchor(
        "task", "Prepare a legacy handoff", goal="Render the unbound handoff safely",
        mode="handoff", in_scope=["handoff"], acceptance_criteria=["Markdown renders."],
        output_format="dict",
    )
    from odibi_anchor._utils._session_state import _SESSION_STATE

    _SESSION_STATE.active_project = None
    _SESSION_STATE.learning_owner_project_id = None
    markdown = anchor("snapshot", mode="handoff", output_format="markdown")

    assert "# Handoff:" in markdown
    assert "Canonical authority" not in markdown
