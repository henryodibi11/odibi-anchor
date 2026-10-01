"""Dispatcher integration for simulated Databricks Git Folder evidence."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.workflow_fixtures import (
    accept_fixture_plan,
    init_source_runtime as init,
    source_workflow_kwargs,
)


class SimulatedDatabricksProvider:
    provider_id = "test.databricks-repos"

    def __init__(self) -> None:
        self.identity = SimpleNamespace(
            repository_id="42",
            workspace_path="/Users/test@example.invalid/odibi_anchor",
            branch="main",
            head_sha="a" * 40,
            remote_url="https://example.invalid/repository.git",
            git_provider="gitHub",
            evidence_kind="databricks_git_folder",
        )
        self.calls = 0

    def capture_identity(self, _target_worktree: str | Path) -> SimpleNamespace:
        self.calls += 1
        return self.identity


class NonCopyableDatabricksProvider(SimulatedDatabricksProvider):
    def __deepcopy__(self, _memo):
        raise Exception("live provider must not be deep-copied")


def current_session_state():
    from odibi_anchor._utils._session_state import _SESSION_STATE

    return _SESSION_STATE


def reset_current_session() -> None:
    from odibi_anchor._utils._session_state import reset_session_state
    from odibi_anchor.codebase.structured_learning_context import (
        active_learning_obligation,
        close_learning_obligation_legacy,
    )

    state = current_session_state()
    owner = {
        "project_id": state.learning_owner_project_id,
        "task_window_id": state.task_window_id,
    }
    if all(owner.values()):
        active = active_learning_obligation(**owner)
        if active is not None:
            close_learning_obligation_legacy(active["obligation_id"], **owner)
    reset_session_state()


@pytest.fixture(autouse=True)
def restore_source_runtime_after_bootstrap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Keep bootstrap's deliberate module reload from retaining a temporary ANCHOR_HOME."""
    yield
    reset_current_session()
    source_home = tmp_path / "source-anchor-home"
    monkeypatch.setenv("ANCHOR_HOME", str(source_home))
    monkeypatch.setenv("ANCHOR_MEMORY_DB", str(source_home / ".agent_memory.db"))
    init(root=tmp_path, output_format="dict")
    reset_current_session()


def accept_source_task(anchor, *, repository_scope: list[str] | None = None) -> dict:
    repository_scope = repository_scope or ["source.py"]
    anchor("status", output_format="dict")
    anchor("memory", output_format="dict")
    anchor("audit_history", output_format="dict")
    anchor("new_session", name="databricks-git-folder", inline=True, output_format="dict")
    task = anchor(
        "task",
        "Implement bounded Databricks Git Folder source changes.",
        goal="Update only the explicitly authorized source paths.",
        mode="implementation",
        work_type="change",
        execution_mode="source_change",
        risk="low",
        rigor="direct",
        current_state="The Git Folder source file exists but Git dirtiness is unknowable.",
        desired_outcome="Task-scoped bytes change with stable host identity.",
        constraints=["Do not claim local Git cleanliness or PR readiness."],
        known_facts=["Databricks Repos identity is available read-only."],
        evidence=[{"source": "simulated_repos", "observation": "branch and HEAD attested"}],
        in_scope=repository_scope,
        out_of_scope=["Repos writes", "PR readiness"],
        risks=["Concurrent file or host identity drift."],
        acceptance_criteria=["Exact task-start bytes and final scoped changes are reported."],
        stop_conditions=["Branch, HEAD, or acknowledged bytes move unexpectedly."],
        deliverables=["Bounded source edits and transparent delivery limitations."],
        repository_scope=repository_scope,
        accept_unknown_git_state=True,
        output_format="dict",
        **source_workflow_kwargs(repository_scope),
    )
    accept_fixture_plan(anchor)
    return task


def initialize_repository(root: Path) -> str:
    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=root, check=True, capture_output=True,
            text=True, encoding="utf-8",
        ).stdout.strip()

    git("init", "-b", "main")
    git("config", "user.email", "tests@example.invalid")
    git("config", "user.name", "Tests")
    git("config", "commit.gpgsign", "false")
    git("add", ".")
    git("commit", "-m", "base")
    return git("rev-parse", "HEAD")


def test_canonical_local_git_takes_precedence_over_databricks_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    anchor_home = tmp_path.parent / f"{tmp_path.name}-state"
    anchor_home.mkdir()
    monkeypatch.setenv("ANCHOR_HOME", str(anchor_home))
    provider = NonCopyableDatabricksProvider()
    (tmp_path / "source.py").write_text("VALUE = 1\n", encoding="utf-8")
    head_sha = initialize_repository(tmp_path)
    anchor, _, _ = init(root=str(tmp_path), output_format="dict", repository_provider=provider)

    task = accept_source_task(anchor)

    baseline = current_session_state().task_repository_baseline
    assert baseline is not None
    assert baseline.authority_kind == "clean"
    assert baseline.task_start_head_sha == head_sha
    assert current_session_state().repository_provider is provider
    assert provider.calls == 0
    assert "databricks_implementation_guidance" not in task
    assert "repository_evidence" not in task
    authority = task["source_authority"]
    assert authority["status"] == "available_local_git"
    assert authority["capabilities"]["local_worktree_status"] == "available"
    assert authority["capabilities"]["pr_readiness"] == "not_verified"
    reset_current_session()


def test_source_task_requires_scope_and_explicit_unknown_state_acceptance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANCHOR_HOME", str(tmp_path))
    provider = NonCopyableDatabricksProvider()
    (tmp_path / "source.py").write_text("VALUE = 1\n", encoding="utf-8")
    anchor, _, _ = init(root=tmp_path, output_format="dict", repository_provider=provider)
    anchor("status", output_format="dict")
    anchor("memory", output_format="dict")
    anchor("audit_history", output_format="dict")
    anchor("new_session", name="databricks-rejection", inline=True, output_format="dict")

    with pytest.raises(RuntimeError, match="repository_scope is required"):
        anchor(
            "task",
            "Attempt an unbounded Databricks source change.",
            goal="This must fail before source authority is granted.",
            mode="implementation",
            work_type="change",
            execution_mode="source_change",
            risk="low",
            rigor="direct",
            acceptance_criteria=["No unbounded baseline is accepted."],
            accept_unknown_git_state=True,
            output_format="dict",
        )

    assert current_session_state().task_repository_baseline is None
    assert current_session_state().repository_provider is provider
    reset_current_session()


def test_bounded_edit_requires_acknowledgement_and_detects_later_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANCHOR_HOME", str(tmp_path))
    provider = NonCopyableDatabricksProvider()
    source = tmp_path / "source.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    anchor, _, _ = init(root=tmp_path, output_format="dict", repository_provider=provider)

    task = accept_source_task(anchor)

    baseline = current_session_state().task_repository_baseline
    assert getattr(baseline, "evidence_kind", None) == "databricks_git_folder"
    assert baseline.identity_provider is provider
    assert current_session_state().repository_provider is provider
    assert task["repository_evidence"]["evidence_kind"] == "databricks_git_folder"
    assert task["repository_evidence"]["capabilities"]["local_worktree_status"] == "unavailable"
    assert "content" not in task["repository_evidence"]["preimages"][0]
    assert task["source_authority"]["status"] == "available_databricks_git_folder"
    assert task["source_authority"]["capabilities"] == {
        "host_repository_identity": "available",
        "local_worktree_status": "unavailable",
        "git_changed_paths_and_diff": "unavailable",
        "merge_base_and_history": "unavailable",
        "task_scoped_content_diff": "available",
        "task_scoped_write_tracking": "available",
        "pr_readiness": "unavailable",
    }
    # Task baseline plus plan acceptance independently recheck host identity.
    assert provider.calls == 5

    with pytest.raises(RuntimeError, match="repository_scope is required"):
        anchor(
            "task",
            "Attempt a replacement without bounded Databricks source authority.",
            goal="Reject the replacement and preserve the accepted task baseline.",
            mode="implementation",
            work_type="change",
            execution_mode="source_change",
            risk="low",
            rigor="direct",
            acceptance_criteria=["The accepted source baseline remains authoritative."],
            accept_unknown_git_state=True,
            output_format="dict",
        )

    restored_baseline = current_session_state().task_repository_baseline
    assert restored_baseline == baseline
    assert restored_baseline.identity_provider is provider
    assert current_session_state().repository_provider is provider
    anchor("known_bad", changed_files=["source.py"], output_format="dict")

    source.write_text("VALUE = 2\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="unacknowledged scoped filesystem drift"):
        anchor("review", output_format="dict")

    anchor("touched", "source.py", output_format="dict")
    preflight = anchor("preflight", output_format="dict")
    review = anchor("review", output_format="dict")
    assert preflight["metrics"]["scope_source"] == "databricks_git_folder"
    assert preflight["metrics"]["repository_capabilities"]["local_worktree_status"] == "unavailable"
    assert review["metrics"]["scope_source"] == "databricks_git_folder"
    assert review["metrics"]["repository_capabilities"]["pr_readiness"] == "unavailable"
    assert any("task-start-to-current bytes" in risk for risk in review["risks"])
    assert current_session_state().task_repository_write_fingerprints["source.py"]

    source.write_text("VALUE = 3\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="changed concurrently"):
        anchor("review", output_format="dict")
    reset_current_session()


def test_canonical_handoff_rebinds_dirty_databricks_task_with_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    anchor_home = tmp_path / "state"
    target = tmp_path / "target"
    anchor_home.mkdir()
    target.mkdir()
    source = target / "source.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    monkeypatch.setenv("ANCHOR_HOME", str(anchor_home))
    monkeypatch.setenv("ANCHOR_MEMORY_DB", str(anchor_home / ".agent_memory.db"))
    from odibi_anchor._dispatcher._project import project_action, resolve_route_binding

    project_action(
        anchor_home, "create", name="databricks-handoff", target=target, output_format="dict",
    )
    binding = resolve_route_binding(
        anchor_home, project="databricks-handoff", runtime_instance_id="test:origin",
    )
    provider = NonCopyableDatabricksProvider()
    anchor, _, _ = init(
        route_binding=binding, repository_provider=provider, output_format="dict",
    )
    accepted = accept_source_task(anchor)
    task_window_id = accepted["accepted_task_authority"]["task_window_id"]
    anchor("known_bad", changed_files=["source.py"], output_format="dict")
    source.write_text("VALUE = 2\n", encoding="utf-8")
    anchor("touched", "source.py", output_format="dict")

    handoff = anchor("snapshot", mode="handoff", persist=False, output_format="dict")
    assert handoff["schema_version"] == "3.0"
    assert handoff["first_action"]["action"] == "task_rebind"
    rebound, _, _ = init(route_binding=binding, repository_provider=provider, output_format="dict")
    restored = eval(handoff["first_action"]["copy_ready"], {"anchor": rebound})

    assert restored["status"] == "rebound"
    assert restored["task_window_id"] == task_window_id
    assert current_session_state().workflow_binding["workflow_id"] == accepted["workflow"]["state"]["workflow_id"]
    assert current_session_state().repository_provider is provider
    assert current_session_state().task_repository_baseline.identity_provider is provider
    reset_current_session()


def test_python_implementation_lifecycle_gates_and_closes_learning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANCHOR_HOME", str(tmp_path))
    provider = NonCopyableDatabricksProvider()
    source = tmp_path / "source.py"
    test_source = tmp_path / "test_source.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    test_source.write_text("from source import VALUE\n\ndef test_value():\n    assert VALUE == 1\n", encoding="utf-8")
    anchor, _, _ = init(root=tmp_path, output_format="dict", repository_provider=provider)

    task = accept_source_task(anchor, repository_scope=["source.py", "test_source.py"])
    guidance = task["databricks_implementation_guidance"]
    assert 'mode="implementation"' in guidance["invocation"]
    assert "accept_unknown_git_state=True" in guidance["invocation"]
    assert "do not fall back to documentation mode" in guidance["stale_agent_recovery"]
    for required in task["required_skills"]:
        if required["path"] != "n/a":
            anchor("skill_loaded", required["skill"], output_format="dict")

    observation = anchor(
        "learning", "capture",
        observation_type="reusable_practice",
        summary="Task-scoped Databricks bytes provide truthful source evidence.",
        signal_key="databricks.task-scoped-source-evidence",
        impact="medium",
        applicability_scope="workbench",
        evidence=[{"reference_type": "test", "reference": (
            "tests/_dispatcher/test_databricks_repository_integration.py::"
            "test_python_implementation_lifecycle_gates_and_closes_learning"
        )}],
        output_format="dict",
    )

    anchor("known_bad", changed_files=["source.py", "test_source.py"], output_format="dict")
    source.write_text("VALUE = 2\n", encoding="utf-8")
    test_source.write_text("from source import VALUE\n\ndef test_value():\n    assert VALUE == 2\n", encoding="utf-8")
    anchor("touched", "source.py", output_format="dict")
    anchor("touched", "test_source.py", output_format="dict")
    anchor("preflight", output_format="dict")
    verification = anchor("test", target="test_source.py", output_format="dict")
    anchor("review", output_format="dict")
    gate = anchor("gate", output_format="dict")
    learning = anchor(
        "learning", "assess", outcome="observations_recorded",
        observation_ids=[observation["item"]["item_id"]], output_format="dict",
    )

    assert verification["metrics"]["passed"] == 1
    assert verification["metrics"]["exit_code"] == 0
    assert gate["kind"] == "workflow_gate_context"
    assert gate["obligations"] == []
    assert learning["assessment"]["outcome"] == "observations_recorded"
    reset_current_session()


def test_bounded_task_recovers_multiple_drifted_paths_incrementally(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANCHOR_HOME", str(tmp_path))
    provider = NonCopyableDatabricksProvider()
    source = tmp_path / "source.py"
    notes = tmp_path / "notes.txt"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    notes.write_text("old\n", encoding="utf-8")
    anchor, _, _ = init(root=tmp_path, output_format="dict", repository_provider=provider)
    accept_source_task(anchor, repository_scope=["source.py", "notes.txt"])
    anchor("known_bad", changed_files=["source.py"], output_format="dict")

    source.write_text("VALUE = 2\n", encoding="utf-8")
    notes.write_text("new\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="unacknowledged scoped filesystem drift"):
        anchor("review", output_format="dict")

    anchor("touched", "source.py", output_format="dict")
    with pytest.raises(RuntimeError, match="notes.txt"):
        anchor("review", output_format="dict")

    anchor("touched", "notes.txt", output_format="dict")
    review = anchor("review", output_format="dict")

    assert review["metrics"]["scope_source"] == "databricks_git_folder"
    assert set(current_session_state().task_repository_write_fingerprints) == {
        "notes.txt",
        "source.py",
    }
    reset_current_session()


def test_active_bounded_task_still_requires_known_bad_before_python_touched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANCHOR_HOME", str(tmp_path))
    provider = NonCopyableDatabricksProvider()
    source = tmp_path / "source.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    anchor, _, _ = init(root=tmp_path, output_format="dict", repository_provider=provider)
    accept_source_task(anchor)
    source.write_text("VALUE = 2\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="known-bad check first"):
        anchor("touched", "source.py", output_format="dict")

    assert current_session_state().task_repository_write_fingerprints == {}
    reset_current_session()


def test_read_only_and_artifact_operations_do_not_require_source_baseline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANCHOR_HOME", str(tmp_path))
    provider = NonCopyableDatabricksProvider()
    anchor, _, _ = init(root=tmp_path, output_format="dict", repository_provider=provider)

    status = anchor("status", output_format="dict")
    anchor("memory", output_format="dict")
    anchor("audit_history", output_format="dict")
    anchor("new_session", name="databricks-project", inline=True, output_format="dict")
    anchor(
        "task",
        "Create approved Odibi Anchor managed project metadata.",
        goal="Provision the complete managed project contract without source edits.",
        mode="implementation",
        work_type="change",
        execution_mode="artifact_only",
        risk="low",
        rigor="direct",
        acceptance_criteria=["The descriptor, layout, activation, and routing are Anchor-owned."],
        output_format="dict",
    )
    project = anchor(
        "project",
        "create",
        name="Databricks Analysis",
        output_format="dict",
    )

    assert status["kind"] == "session_status"
    assert project["created"] is True
    assert current_session_state().task_repository_baseline is None
    assert current_session_state().repository_provider is provider
    assert provider.calls == 0
    reset_current_session()


def test_failed_task_restores_independent_mutable_state_with_live_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANCHOR_HOME", str(tmp_path))
    provider = NonCopyableDatabricksProvider()
    anchor, _, _ = init(root=tmp_path, output_format="dict", repository_provider=provider)
    anchor("status", output_format="dict")
    anchor("memory", output_format="dict")
    anchor("audit_history", output_format="dict")
    anchor("new_session", name="rollback-provider", inline=True, output_format="dict")

    state = current_session_state()
    original_tags = ["before"]
    state.task_tags = original_tags

    from odibi_anchor._dispatcher import _post_dispatch

    real_post_dispatch = _post_dispatch.run_post_dispatch

    def fail_after_post_dispatch(*args, **kwargs):
        result = real_post_dispatch(*args, **kwargs)
        kwargs["session_state"].task_tags.append("mutated-after-snapshot")
        raise RuntimeError("simulated post-dispatch failure")

    monkeypatch.setattr(_post_dispatch, "run_post_dispatch", fail_after_post_dispatch)

    with pytest.raises(RuntimeError, match="simulated post-dispatch failure"):
        anchor(
            "task",
            "Create an artifact-only task whose post-dispatch step fails.",
            goal="Prove rollback restores mutable task state independently.",
            mode="implementation",
            work_type="change",
            execution_mode="artifact_only",
            risk="low",
            rigor="direct",
            acceptance_criteria=["The pre-task tags are restored without aliasing."],
            output_format="dict",
        )

    assert state.task_tags == ["before"]
    assert state.task_tags is not original_tags
    assert state.repository_provider is provider
    reset_current_session()


@pytest.mark.parametrize("execution_mode", ["read_only", "artifact_only", "data_change"])
@pytest.mark.parametrize("target_kind", ["plain", "local_git", "git_folder"])
def test_non_source_task_reports_unprobed_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, execution_mode: str, target_kind: str,
) -> None:
    home = tmp_path.parent / f"{tmp_path.name}-home"
    home.mkdir()
    monkeypatch.setenv("ANCHOR_HOME", str(home))
    (tmp_path / "source.py").write_text("VALUE = 1\n", encoding="utf-8")
    if target_kind == "local_git":
        initialize_repository(tmp_path)
    provider = NonCopyableDatabricksProvider() if target_kind == "git_folder" else None
    anchor, _, _ = init(root=tmp_path, output_format="dict", repository_provider=provider)
    anchor("status", output_format="dict")
    anchor("audit_history", output_format="dict")
    anchor("new_session", name="authority-projection", inline=True, output_format="dict")
    task = anchor(
        "task", "Verify explicit source authority diagnostics.",
        goal="Inspect task capabilities without probing source providers.",
        mode="implementation", work_type="change", execution_mode=execution_mode,
        risk="low", rigor="direct", acceptance_criteria=["No source baseline captured."],
        output_format="dict",
    )
    authority = task["source_authority"]
    assert authority["status"] == "not_requested"
    assert "not probed" in authority["reason"]
    assert set(authority["capabilities"].values()) == {"not_requested"}
    assert 'execution_mode="artifact_only"' in authority["guidance"]["artifact_only_invocation"]
    assert "artifact_only cannot write target source" in authority["guidance"]["boundary"]
    assert current_session_state().task_repository_baseline is None
    if provider is not None:
        assert provider.calls == 0


@pytest.mark.parametrize("rejected_provider", [False, True])
def test_plain_folder_source_rejection_has_actionable_guidance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rejected_provider: bool,
) -> None:
    from odibi_anchor.operational._databricks import DatabricksGitFolderRepositoryProvider

    provider = DatabricksGitFolderRepositoryProvider(
        lambda operation, parameters: {"object_type": "DIRECTORY", "is_git_folder": False},
        workspace_path="/Users/test/plain-folder", target_worktree=tmp_path,
    )

    home = tmp_path.parent / f"{tmp_path.name}-home"
    home.mkdir()
    monkeypatch.setenv("ANCHOR_HOME", str(home))
    (tmp_path / "source.py").write_text("VALUE = 1\n", encoding="utf-8")
    anchor, _, _ = init(
        root=tmp_path, output_format="dict",
        repository_provider=provider if rejected_provider else None,
    )
    with pytest.raises(RuntimeError) as error:
        accept_source_task(anchor)
    message = str(error.value)
    assert 'execution_mode="artifact_only"' in message
    assert 'execution_mode="source_change"' in message
    assert "repository_provider" in message
    assert "repository_scope" in message
    assert "accept_unknown_git_state=True" in message
    assert "artifact_only cannot write target source" in message
    assert "Git Folder conversion is only needed for source changes" in message
    assert ("provider rejected the target: not a Databricks Git Folder" in message) is rejected_provider
    assert current_session_state().task_repository_baseline is None


def test_source_provider_error_does_not_disclose_arbitrary_message(tmp_path: Path) -> None:
    from odibi_anchor._repository_snapshot import _provider_identity
    from odibi_anchor.operational._contract import ContractError

    class FailedProvider:
        def capture_identity(self, target):
            raise ContractError("private-api-response-with-secret")

    with pytest.raises(RuntimeError) as error:
        _provider_identity(FailedProvider(), tmp_path)
    assert "read-only host identity acquisition failed" in str(error.value)
    assert "private-api-response-with-secret" not in str(error.value)


def test_guidance_invocations_and_docs_match_profiles() -> None:
    import ast

    from odibi_anchor._repository_snapshot import source_authority_guidance
    from odibi_anchor.planning._task_profile import normalize_task_profile

    guidance = source_authority_guidance()
    for key, expected in [("artifact_only_invocation", "artifact_only"),
                          ("git_folder_invocation", "source_change")]:
        call = ast.parse(guidance[key], mode="eval").body
        kwargs = {kw.arg: ast.literal_eval(kw.value) for kw in call.keywords}
        profile = normalize_task_profile(
            legacy_mode=kwargs["mode"], work_type=kwargs["work_type"],
            execution_mode=kwargs["execution_mode"],
        )
        assert profile.execution_mode == expected
    docs = (Path(__file__).parents[2] / "docs/guides/getting-started.md").read_text()
    from odibi_anchor.bootstrap import init as public_init

    for text in (docs, public_init.__doc__):
        assert 'execution_mode="artifact_only"' in text
        assert 'execution_mode="source_change"' in text
        assert "artifact_only cannot write target source" in text.replace("`", "")
        assert "repository_scope" in text
        assert "accept_unknown_git_state=True" in text
