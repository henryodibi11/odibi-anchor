"""Real-Git coverage for local repository snapshot evidence."""

from __future__ import annotations

import subprocess
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

import odibi_anchor._repository_snapshot as repository_snapshot
from odibi_anchor._repository_snapshot import (
    DatabricksGitFolderIdentity,
    DatabricksGitFolderTaskBaseline,
    DatabricksTaskChangeScope,
    TaskChangeScope,
    TaskRepositoryBaseline,
    UnbornTaskRepositoryBaseline,
    acknowledge_databricks_task_writes,
    capture_databricks_git_folder_task_baseline,
    capture_repository_snapshot,
    capture_task_change_scope,
    capture_task_repository_baseline,
    databricks_task_baseline_projection,
    managed_repository_fingerprint,
    task_scope_review_diff,
    validate_repository_snapshot,
    verify_databricks_task_preconditions,
)


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True,
                          text=True, encoding="utf-8").stdout.strip()


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-b", "main")
    git(tmp_path, "config", "user.email", "tests@example.invalid")
    git(tmp_path, "config", "user.name", "Tests")
    git(tmp_path, "config", "commit.gpgsign", "false")
    (tmp_path / "a.py").write_text('"""base."""\nVALUE = 1\n', encoding="utf-8")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-m", "base")
    git(tmp_path, "switch", "-c", "feature")
    return tmp_path


@pytest.fixture
def unborn_repository(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-b", "main")
    git(tmp_path, "config", "user.email", "tests@example.invalid")
    git(tmp_path, "config", "user.name", "Tests")
    git(tmp_path, "config", "commit.gpgsign", "false")
    return tmp_path


def repository_state(root: Path) -> dict[str, object]:
    """Capture source, index, ref, and status bytes without requiring a commit."""
    git_dir = root / ".git"
    refs = tuple(
        (path.relative_to(git_dir).as_posix(), path.read_bytes())
        for path in sorted((git_dir / "refs").rglob("*"))
        if path.is_file()
    )
    files = tuple(
        (path.relative_to(root).as_posix(), path.read_bytes())
        for path in sorted(root.rglob("*"))
        if path.is_file() and ".git" not in path.relative_to(root).parts
    )
    status = git(root, "status", "--porcelain=v2", "--branch")
    staged = git(root, "diff", "--cached", "--binary")
    unstaged = git(root, "diff", "--binary")
    # Read the index after Git inspection has completed benign stat refreshes.
    index = git_dir / "index"
    return {
        "head": (git_dir / "HEAD").read_bytes(),
        "index": index.read_bytes() if index.is_file() else None,
        "refs": refs,
        "files": files,
        "status": status,
        "staged": staged,
        "unstaged": unstaged,
    }


def test_git_subprocess_does_not_inherit_mcp_stdio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        captured.update(kwargs)
        return subprocess.CompletedProcess(args, 0, stdout=b"git version 2.0\n", stderr=b"")

    monkeypatch.setattr(repository_snapshot.subprocess, "run", run)

    assert repository_snapshot._Git(tmp_path).run_bytes("--version") == b"git version 2.0\n"
    assert captured["stdin"] is subprocess.DEVNULL


def test_aggregate_committed_staged_unstaged_untracked_and_ranges(repository: Path) -> None:
    (repository / "a.py").write_text('"""base."""\nVALUE = 2\n', encoding="utf-8")
    git(repository, "add", "a.py")
    (repository / "a.py").write_text('"""base."""\nVALUE = 3\n', encoding="utf-8")
    (repository / "new.txt").write_text("one\ntwo\n", encoding="utf-8")
    snapshot = capture_repository_snapshot(repository, "main")
    assert snapshot.staged_paths == ("a.py",)
    assert snapshot.unstaged_paths == ("a.py",)
    assert snapshot.untracked_paths == ("new.txt",)
    assert {(item.path, item.start, item.end) for item in snapshot.changed_line_ranges} == {
        ("a.py", 2, 2), ("new.txt", 1, 2)}


def test_task_scope_unions_committed_staged_unstaged_untracked(repository: Path) -> None:
    baseline = capture_task_repository_baseline(repository, "main")
    (repository / "committed.py").write_text("COMMITTED = 1\n", encoding="utf-8")
    git(repository, "add", "committed.py")
    git(repository, "commit", "-m", "task commit")
    (repository / "staged.py").write_text("STAGED = 1\n", encoding="utf-8")
    git(repository, "add", "staged.py")
    (repository / "a.py").write_text('"""base."""\nVALUE = 2\n', encoding="utf-8")
    (repository / "untracked.py").write_text("UNTRACKED = 1\n", encoding="utf-8")

    scope = capture_task_change_scope(baseline)

    assert set(scope.changed_paths) == {"a.py", "committed.py", "staged.py", "untracked.py"}
    assert scope.provenance["created_paths"] == (
        "committed.py", "staged.py", "untracked.py",
    )
    assert task_scope_review_diff(scope)["samples"]["per_file"]["a.py"]["status"] == "modified"
    assert scope.provenance["scope_source"] == "task_repository_baseline"


def test_clean_feature_branch_pretask_commit_is_accepted_and_excluded(repository: Path) -> None:
    (repository / "before.py").write_text("BEFORE = 1\n", encoding="utf-8")
    git(repository, "add", "before.py")
    git(repository, "commit", "-m", "before task")
    baseline = capture_task_repository_baseline(repository, "main")
    assert isinstance(baseline, TaskRepositoryBaseline)
    assert capture_task_change_scope(baseline).changed_paths == ()


@pytest.mark.parametrize("dirty_kind", ["staged", "unstaged", "untracked"])
def test_task_baseline_rejects_each_dirty_class(repository: Path, dirty_kind: str) -> None:
    if dirty_kind == "untracked":
        (repository / "new.py").write_text("NEW = 1\n", encoding="utf-8")
    else:
        (repository / "a.py").write_text("VALUE = 2\n", encoding="utf-8")
        if dirty_kind == "staged":
            git(repository, "add", "a.py")
    with pytest.raises(
        RuntimeError,
        match=r"^BLOCKED: source-change task requires a clean initial Git worktree$",
    ):
        capture_task_repository_baseline(repository, "main")


def test_task_scope_rejects_branch_switch_and_reports_target_drift(repository: Path) -> None:
    baseline = capture_task_repository_baseline(repository, "main")
    git(repository, "branch", "moved-main", "main")
    git(repository, "switch", "main")
    (repository / "target.txt").write_text("move\n", encoding="utf-8")
    git(repository, "add", "target.txt")
    git(repository, "commit", "-m", "move target")
    git(repository, "switch", "feature")
    with pytest.raises(RuntimeError, match="not integrated"):
        capture_task_change_scope(baseline)
    git(repository, "merge", "--no-edit", "main")
    scope = capture_task_change_scope(baseline)
    assert scope.provenance["target_drift"] is True
    assert scope.provenance["target_start_sha"] == baseline.target_sha
    assert scope.provenance["target_current_sha"] == git(repository, "rev-parse", "main")
    git(repository, "switch", "moved-main")
    with pytest.raises(RuntimeError, match="branch switched"):
        capture_task_change_scope(baseline)


def test_deletion_rename_binary_and_quoted_path_are_identities(repository: Path) -> None:
    git(repository, "mv", "a.py", "legal name file.py")
    (repository / "bytes.bin").write_bytes(b"\x00\xff")
    git(repository, "add", ".")
    snapshot = capture_repository_snapshot(repository, "main")
    assert "legal name file.py" in snapshot.changed_paths
    assert "bytes.bin" in snapshot.changed_paths
    assert not any(item.path == "bytes.bin" for item in snapshot.changed_line_ranges)


def test_colocated_exact_managed_exclusion_never_hides_tracked(repository: Path) -> None:
    managed_paths = (
        "PROJECT.md", "source/input.md", "notebooks/EVD-1.md", "problems/PRB-1.md",
        "specs/X_SPEC.md", "work_items/WI-1.md", "decisions/DEC-1.md", "archive/old.md",
        "pull_requests/draft.md", ".odibi-anchor/state.json",
    )
    for relative in managed_paths:
        path = repository / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("generated", encoding="utf-8")
    (repository / "pull_requests-user").mkdir()
    (repository / "pull_requests-user" / "source.py").write_text("x = 1\n", encoding="utf-8")
    snapshot = capture_repository_snapshot(repository, "main", artifact_root=repository)
    assert not set(managed_paths) & set(snapshot.changed_paths)
    assert "pull_requests-user/source.py" in snapshot.changed_paths
    assert snapshot.provenance["managed_fingerprint"]


def test_generated_artifact_requires_authorized_fingerprint_and_tamper_stales(repository: Path) -> None:
    snapshot = capture_repository_snapshot(repository, "main", artifact_root=repository)
    (repository / "pull_requests").mkdir()
    (repository / "pull_requests" / "draft.md").write_text("generated", encoding="utf-8")
    assert validate_repository_snapshot(snapshot) == (False, ("managed",))
    authorized = managed_repository_fingerprint(snapshot)
    assert validate_repository_snapshot(snapshot, authorized_managed_fingerprint=authorized) == (True, ())
    (repository / "pull_requests" / "draft.md").write_text("tampered", encoding="utf-8")
    assert "managed" in validate_repository_snapshot(
        snapshot, authorized_managed_fingerprint=authorized)[1]
    (repository / "a.py").write_text('"""base."""\nVALUE = 9\n', encoding="utf-8")
    assert validate_repository_snapshot(snapshot)[0] is False


def test_detached_head_and_stale_head_target(repository: Path) -> None:
    git(repository, "checkout", "--detach")
    snapshot = capture_repository_snapshot(repository, "main")
    assert snapshot.branch is None
    (repository / "x").write_text("x", encoding="utf-8")
    git(repository, "add", "x")
    git(repository, "commit", "-m", "move head")
    assert "head" in validate_repository_snapshot(snapshot)[1]


@pytest.mark.parametrize("ref", ["-main", "main~1", "bad ref", "refs/heads/a:bad"])
def test_unsafe_target_ref_rejected(repository: Path, ref: str) -> None:
    with pytest.raises(ValueError, match="unsafe"):
        capture_repository_snapshot(repository, ref)


def test_pristine_unborn_target_branch_returns_distinct_frozen_baseline_without_mutation(
    unborn_repository: Path,
) -> None:
    before = repository_state(unborn_repository)

    with pytest.raises(RuntimeError, match="git rev-parse HEAD"):
        capture_repository_snapshot(unborn_repository, "main")
    baseline = capture_task_repository_baseline(unborn_repository, "main")

    assert isinstance(baseline, UnbornTaskRepositoryBaseline)
    assert baseline.target_worktree == str(unborn_repository)
    assert baseline.branch == baseline.configured_target_ref == "main"
    assert not any(
        hasattr(baseline, name)
        for name in ("head_sha", "target_sha", "merge_base_sha", "task_start_head_sha")
    )
    scope = capture_task_change_scope(baseline)
    assert isinstance(scope, TaskChangeScope)
    assert scope.changed_paths == ()
    assert scope.provenance["created_paths"] == ()
    assert scope.provenance["target_current_sha"] is None
    assert scope.provenance["target_birth"] is False
    with pytest.raises(FrozenInstanceError):
        baseline.branch = "other"  # type: ignore[misc]
    assert repository_state(unborn_repository) == before


def test_snapshot_detects_empty_file_intent_to_add_becoming_staged(repository: Path) -> None:
    empty = repository / "empty.txt"
    empty.touch()
    git(repository, "add", "--intent-to-add", "empty.txt")
    snapshot = capture_repository_snapshot(repository, "main")
    assert snapshot.staged_paths == ()
    assert snapshot.unstaged_paths == ("empty.txt",)

    git(repository, "add", "empty.txt")

    valid, stale = validate_repository_snapshot(snapshot)
    assert valid is False
    assert "index" in stale


@pytest.mark.parametrize("dirty_kind", ["untracked", "staged", "unstaged", "staged+unstaged"])
def test_unborn_baseline_rejects_every_mutable_path_class(
    unborn_repository: Path,
    dirty_kind: str,
) -> None:
    source = unborn_repository / "source.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    if dirty_kind == "staged":
        git(unborn_repository, "add", "source.py")
    elif dirty_kind == "unstaged":
        git(unborn_repository, "add", "--intent-to-add", "source.py")
    elif dirty_kind == "staged+unstaged":
        git(unborn_repository, "add", "source.py")
        source.write_text("VALUE = 2\n", encoding="utf-8")
    before = repository_state(unborn_repository)

    with pytest.raises(
        RuntimeError,
        match=r"^BLOCKED: source-change task requires a clean initial Git worktree$",
    ):
        capture_task_repository_baseline(unborn_repository, "main")

    assert repository_state(unborn_repository) == before


@pytest.mark.parametrize(
    ("branch", "target"),
    [("trunk", "main"), ("main", "origin/main"), ("main", "refs/heads/main"), ("main", "Main")],
)
def test_unborn_baseline_requires_exact_short_target_branch(
    tmp_path: Path,
    branch: str,
    target: str,
) -> None:
    git(tmp_path, "init", "-b", branch)
    with pytest.raises(RuntimeError, match="current branch to equal the configured target"):
        capture_task_repository_baseline(tmp_path, target)


def test_unborn_baseline_rejects_resolvable_ambiguous_target(unborn_repository: Path) -> None:
    empty_tree = git(unborn_repository, "hash-object", "-t", "tree", "/dev/null")
    imported_commit = git(unborn_repository, "commit-tree", empty_tree, "-m", "imported")
    git(unborn_repository, "tag", "main", imported_commit)

    with pytest.raises(RuntimeError, match=r"current branch|absent configured target"):
        capture_task_repository_baseline(unborn_repository, "main")


def test_operational_git_error_is_never_classified_as_unborn(unborn_repository: Path) -> None:
    (unborn_repository / ".git" / "HEAD").write_text("invalid-head\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match=r"local git command failed \(128\)") as raised:
        capture_task_repository_baseline(unborn_repository, "main")
    assert not str(raised.value).startswith("BLOCKED:")


def test_non_git_source_baseline_reports_capability_specific_blocker(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError) as raised:
        capture_task_repository_baseline(tmp_path, "main")

    message = str(raised.value)
    assert message.startswith("BLOCKED: source-change task requires a canonical local Git worktree")
    assert "branch, HEAD, working-tree status, changed paths, merge-base, and history" in message
    assert "Read-only orientation/analysis and approved managed-artifact operations remain allowed" in message
    assert "clean" not in message.lower()


def test_unborn_scope_tracks_untracked_staged_modified_and_binary_files(
    unborn_repository: Path,
) -> None:
    baseline = capture_task_repository_baseline(unborn_repository, "main")
    text = unborn_repository / "source.py"
    binary = unborn_repository / "bytes.bin"
    text.write_text("line one\nline two\n", encoding="utf-8")
    binary.write_bytes(b"\x00\xff")

    untracked = capture_task_change_scope(baseline)
    assert isinstance(untracked, TaskChangeScope)
    assert untracked.untracked_paths == ("bytes.bin", "source.py")
    assert untracked.changed_paths == ("bytes.bin", "source.py")
    assert [(item.path, item.kind, item.start, item.end)
            for item in untracked.changed_line_ranges] == [("source.py", "added", 1, 2)]

    git(unborn_repository, "add", "bytes.bin", "source.py")
    text.write_text("current line\n", encoding="utf-8")
    staged_and_modified = capture_task_change_scope(baseline)
    assert staged_and_modified.staged_paths == ("bytes.bin", "source.py")
    assert staged_and_modified.unstaged_paths == ("source.py",)
    assert staged_and_modified.untracked_paths == ()
    assert staged_and_modified.changed_paths == ("bytes.bin", "source.py")
    assert [(item.path, item.start, item.end)
            for item in staged_and_modified.changed_line_ranges] == [("source.py", 1, 1)]


def test_unborn_scope_survives_first_and_later_commits_with_exact_provenance(
    unborn_repository: Path,
) -> None:
    baseline = capture_task_repository_baseline(unborn_repository, "main")
    (unborn_repository / "first.py").write_text("FIRST = 1\n", encoding="utf-8")
    git(unborn_repository, "add", "first.py")
    git(unborn_repository, "commit", "-m", "first")
    (unborn_repository / "later.py").write_text("LATER = 1\nLATER = 2\n", encoding="utf-8")
    git(unborn_repository, "add", "later.py")
    git(unborn_repository, "commit", "-m", "later")

    scope = capture_task_change_scope(baseline)

    assert isinstance(scope, TaskChangeScope)
    assert scope.changed_paths == ("first.py", "later.py")
    assert scope.provenance["created_paths"] == scope.changed_paths
    assert scope.provenance["scope_source"] == "task_repository_baseline"
    assert scope.provenance["task_start_state"] == "unborn"
    assert scope.provenance["task_start_head_sha"] is None
    assert scope.provenance["target_start_sha"] is None
    assert scope.provenance["target_current_sha"] == git(unborn_repository, "rev-parse", "HEAD")
    assert scope.provenance["target_birth"] is True
    assert scope.provenance["target_drift"] is False
    assert scope.provenance["ancestry_basis"] == "conceptual_empty_start"
    assert not any(
        command["command"][1] == "merge-base"
        for command in scope.provenance["commands"]
    )
    assert [(item.path, item.start, item.end) for item in scope.changed_line_ranges] == [
        ("first.py", 1, 1),
        ("later.py", 1, 2),
    ]
    with pytest.raises(TypeError):
        scope.provenance["target_birth"] = False  # type: ignore[index]
    with pytest.raises(TypeError):
        scope.provenance["commands"][0]["exit_code"] = 99  # type: ignore[index]


def test_unborn_scope_deletion_remains_until_net_current_state_is_empty(
    unborn_repository: Path,
) -> None:
    baseline = capture_task_repository_baseline(unborn_repository, "main")
    source = unborn_repository / "source.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    git(unborn_repository, "add", "source.py")
    git(unborn_repository, "commit", "-m", "first")

    source.unlink()
    unstaged = capture_task_change_scope(baseline)
    assert unstaged.unstaged_paths == ("source.py",)
    assert unstaged.changed_paths == ("source.py",)
    assert unstaged.changed_line_ranges == ()

    git(unborn_repository, "add", "source.py")
    staged = capture_task_change_scope(baseline)
    assert staged.staged_paths == ("source.py",)
    assert staged.changed_paths == ("source.py",)
    assert staged.changed_line_ranges == ()

    git(unborn_repository, "commit", "-m", "delete")
    deleted = capture_task_change_scope(baseline)
    assert deleted.changed_paths == ()
    assert deleted.provenance["created_paths"] == ()


def test_unborn_scope_pending_and_committed_rename_preserves_net_identities(
    unborn_repository: Path,
) -> None:
    baseline = capture_task_repository_baseline(unborn_repository, "main")
    (unborn_repository / "old.py").write_text("VALUE = 1\n", encoding="utf-8")
    git(unborn_repository, "add", "old.py")
    git(unborn_repository, "commit", "-m", "first")

    git(unborn_repository, "mv", "old.py", "new.py")
    pending = capture_task_change_scope(baseline)
    assert pending.staged_paths == ("new.py", "old.py")
    assert pending.changed_paths == ("new.py", "old.py")
    assert [item.path for item in pending.changed_line_ranges] == ["new.py"]

    git(unborn_repository, "commit", "-m", "rename")
    committed = capture_task_change_scope(baseline)
    assert committed.changed_paths == ("new.py",)
    assert committed.provenance["created_paths"] == ("new.py",)
    assert [item.path for item in committed.changed_line_ranges] == ["new.py"]


def test_unborn_scope_broadens_over_imported_history_without_ancestry_claim(
    unborn_repository: Path,
) -> None:
    baseline = capture_task_repository_baseline(unborn_repository, "main")
    (unborn_repository / "first.py").write_text("FIRST = 1\n", encoding="utf-8")
    git(unborn_repository, "add", "first.py")
    git(unborn_repository, "commit", "-m", "first")

    git(unborn_repository, "switch", "--orphan", "imported")
    for path in unborn_repository.iterdir():
        if path.name != ".git" and path.is_file():
            path.unlink()
    (unborn_repository / "foreign.py").write_text("FOREIGN = 1\n", encoding="utf-8")
    git(unborn_repository, "add", "foreign.py")
    git(unborn_repository, "commit", "-m", "foreign")
    foreign_sha = git(unborn_repository, "rev-parse", "HEAD")
    git(unborn_repository, "switch", "main")
    git(unborn_repository, "reset", "--hard", foreign_sha)
    (unborn_repository / "staged.py").write_text("STAGED = 1\n", encoding="utf-8")
    git(unborn_repository, "add", "staged.py")
    (unborn_repository / "untracked.py").write_text("UNTRACKED = 1\n", encoding="utf-8")

    scope = capture_task_change_scope(baseline)

    assert scope.changed_paths == ("foreign.py", "staged.py", "untracked.py")
    assert scope.provenance["created_paths"] == scope.changed_paths
    assert scope.provenance["ancestry_basis"] == "conceptual_empty_start"
    assert not any(
        "merge-base" in command["command"]
        for command in scope.provenance["commands"]
    )


def test_unborn_scope_rejects_branch_switch_and_detach_after_target_birth(
    unborn_repository: Path,
) -> None:
    baseline = capture_task_repository_baseline(unborn_repository, "main")
    (unborn_repository / "source.py").write_text("VALUE = 1\n", encoding="utf-8")
    git(unborn_repository, "add", "source.py")
    git(unborn_repository, "commit", "-m", "first")

    git(unborn_repository, "switch", "-c", "other")
    with pytest.raises(RuntimeError, match="branch switched or detached"):
        capture_task_change_scope(baseline)
    git(unborn_repository, "switch", "main")
    git(unborn_repository, "checkout", "--detach")
    with pytest.raises(RuntimeError, match="branch switched or detached"):
        capture_task_change_scope(baseline)


class StubDatabricksRepositoryProvider:
    provider_id = "test.databricks-repos"

    def __init__(self, identities: list[DatabricksGitFolderIdentity] | None = None) -> None:
        self.identity = DatabricksGitFolderIdentity(
            repository_id="42",
            workspace_path="/Users/test@example.invalid/odibi_anchor",
            branch="main",
            head_sha="a" * 40,
            remote_url="https://example.invalid/repository.git",
            git_provider="gitHub",
        )
        self.identities = list(identities or ())
        self.calls: list[str] = []

    def capture_identity(self, target_worktree: str | Path) -> DatabricksGitFolderIdentity:
        self.calls.append(str(target_worktree))
        if self.identities:
            return self.identities.pop(0)
        return self.identity


def capture_databricks_baseline(
    root: Path,
    provider: StubDatabricksRepositoryProvider,
    scope: list[str],
) -> DatabricksGitFolderTaskBaseline:
    return capture_databricks_git_folder_task_baseline(
        root,
        provider,
        scope,
        accept_unknown_git_state=True,
    )


def test_databricks_baseline_requires_explicit_unknown_git_state_acceptance(
    tmp_path: Path,
) -> None:
    provider = StubDatabricksRepositoryProvider()

    with pytest.raises(RuntimeError, match="accept_unknown_git_state=True"):
        capture_databricks_git_folder_task_baseline(
            tmp_path,
            provider,
            ["source.py"],
        )


def test_databricks_provider_cannot_bypass_local_git_authority(repository: Path) -> None:
    provider = StubDatabricksRepositoryProvider()

    with pytest.raises(RuntimeError, match="select exactly one repository authority"):
        capture_databricks_baseline(repository, provider, ["a.py"])


def test_databricks_projected_git_directory_does_not_compete_with_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = StubDatabricksRepositoryProvider()
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(
        "odibi_anchor._repository_snapshot._canonical_local_git_available",
        lambda _root: False,
    )

    baseline = capture_databricks_baseline(tmp_path, provider, ["source.py"])

    assert baseline.identity.repository_id == "42"
    assert baseline.identity_provider is provider


def test_databricks_baseline_preserves_exact_preimages_without_public_bytes(
    tmp_path: Path,
) -> None:
    provider = StubDatabricksRepositoryProvider()
    source = b"VALUE = 1\r\n\x00binary-tail"
    (tmp_path / "source.py").write_bytes(source)
    (tmp_path / "package").mkdir()
    (tmp_path / "package" / "nested.py").write_text("NESTED = 1\n", encoding="utf-8")

    baseline = capture_databricks_baseline(
        tmp_path,
        provider,
        ["source.py", "package", "future.py"],
    )

    assert [item.path for item in baseline.preimages] == ["package/nested.py", "source.py"]
    assert next(item for item in baseline.preimages if item.path == "source.py").content == source
    assert baseline.directory_scopes == ("package",)
    assert len(provider.calls) == 2
    projection = databricks_task_baseline_projection(baseline)
    assert projection["evidence_kind"] == "databricks_git_folder"
    assert projection["capabilities"]["local_worktree_status"] == "unavailable"
    assert projection["capabilities"]["task_scoped_content_diff"] == "available"
    assert all("content" not in item for item in projection["preimages"])


def test_databricks_scope_rejects_unacknowledged_and_concurrent_drift(
    tmp_path: Path,
) -> None:
    provider = StubDatabricksRepositoryProvider()
    path = tmp_path / "source.py"
    path.write_text("VALUE = 1\n", encoding="utf-8")
    baseline = capture_databricks_baseline(tmp_path, provider, ["source.py"])

    path.write_text("VALUE = 2\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="unacknowledged scoped filesystem drift") as exc_info:
        capture_task_change_scope(baseline)
    message = str(exc_info.value)
    assert "first run anchor('known_bad', changed_files=['source.py'])" in message
    assert message.index("known_bad") < message.index("anchor('touched'")

    fingerprints = acknowledge_databricks_task_writes(baseline, {}, ["source.py"])
    scope = capture_task_change_scope(baseline, fingerprints)
    assert isinstance(scope, DatabricksTaskChangeScope)
    assert scope.changed_paths == ("source.py",)
    assert scope.provenance["working_tree_status"] == "unavailable"
    assert scope.provenance["pr_readiness"] == "unavailable"

    path.write_text("VALUE = 3\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="changed concurrently"):
        capture_task_change_scope(baseline, fingerprints)


def test_databricks_scope_supports_incremental_acknowledgement_of_bounded_drift(
    tmp_path: Path,
) -> None:
    provider = StubDatabricksRepositoryProvider()
    first = tmp_path / "first.py"
    second = tmp_path / "second.txt"
    first.write_text("OLD = 1\n", encoding="utf-8")
    second.write_text("old\n", encoding="utf-8")
    baseline = capture_databricks_baseline(tmp_path, provider, ["first.py", "second.txt"])
    first.write_text("NEW = 2\n", encoding="utf-8")
    second.write_text("new\n", encoding="utf-8")

    verify_databricks_task_preconditions(
        baseline,
        {},
        allow_paths=["first.py"],
        incremental_acknowledgement=True,
    )
    fingerprints = acknowledge_databricks_task_writes(
        baseline,
        {},
        ["first.py"],
        incremental=True,
    )
    with pytest.raises(RuntimeError, match="second.txt"):
        capture_task_change_scope(baseline, fingerprints)

    first.write_text("CONCURRENT = 3\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="first.py"):
        verify_databricks_task_preconditions(
            baseline,
            fingerprints,
            allow_paths=["second.txt"],
            incremental_acknowledgement=True,
        )
    first.write_text("NEW = 2\n", encoding="utf-8")

    verify_databricks_task_preconditions(
        baseline,
        fingerprints,
        allow_paths=["second.txt"],
        incremental_acknowledgement=True,
    )
    fingerprints = acknowledge_databricks_task_writes(
        baseline,
        fingerprints,
        ["second.txt"],
        incremental=True,
    )

    assert capture_task_change_scope(baseline, fingerprints).changed_paths == (
        "first.py",
        "second.txt",
    )


def test_databricks_incremental_acknowledgement_requires_exactly_one_path(
    tmp_path: Path,
) -> None:
    provider = StubDatabricksRepositoryProvider()
    baseline = capture_databricks_baseline(tmp_path, provider, ["first.py", "second.py"])

    for paths in ([], ["first.py", "second.py"]):
        with pytest.raises(RuntimeError, match="exactly one named in-scope path"):
            verify_databricks_task_preconditions(
                baseline,
                {},
                allow_paths=paths,
                incremental_acknowledgement=True,
            )
        with pytest.raises(RuntimeError, match="exactly one named in-scope path"):
            acknowledge_databricks_task_writes(
                baseline,
                {},
                paths,
                incremental=True,
            )


def test_databricks_drift_guidance_is_bounded_and_preserves_non_python_message(
    tmp_path: Path,
) -> None:
    provider = StubDatabricksRepositoryProvider()
    paths = [
        tmp_path / f"file-{index}{'.py' if index in {0, 8} else '.txt'}"
        for index in range(9)
    ]
    for path in paths:
        path.write_text("old\n", encoding="utf-8")
    baseline = capture_databricks_baseline(
        tmp_path,
        provider,
        [path.name for path in paths],
    )
    for path in paths:
        path.write_text("new\n", encoding="utf-8")

    with pytest.raises(RuntimeError) as exc_info:
        capture_task_change_scope(baseline)

    message = str(exc_info.value)
    assert "1 additional path(s) omitted" in message
    assert "file-8.py" not in message
    assert "known_bad" in message
    assert "anchor('touched', '<path>')" in message
    assert message.index("known_bad") < message.index("anchor('touched'")
    assert "handle the displayed paths and retry" in message

    single = tmp_path / "single.txt"
    single.write_text("old\n", encoding="utf-8")
    single_baseline = capture_databricks_baseline(tmp_path, provider, ["single.txt"])
    single.write_text("new\n", encoding="utf-8")
    with pytest.raises(RuntimeError) as single_exc:
        capture_task_change_scope(single_baseline)
    assert "; acknowledge each intended edit with anchor('touched', '<path>')" in str(
        single_exc.value
    )


def test_databricks_scope_tracks_acknowledged_create_edit_and_delete(
    tmp_path: Path,
) -> None:
    provider = StubDatabricksRepositoryProvider()
    (tmp_path / "edit.py").write_text("OLD = 1\n", encoding="utf-8")
    (tmp_path / "delete.py").write_bytes(b"delete me\n")
    baseline = capture_databricks_baseline(
        tmp_path,
        provider,
        ["edit.py", "delete.py", "create.py"],
    )

    (tmp_path / "edit.py").write_text("NEW = 2\n", encoding="utf-8")
    (tmp_path / "delete.py").unlink()
    (tmp_path / "create.py").write_text("CREATED = 1\n", encoding="utf-8")
    fingerprints = acknowledge_databricks_task_writes(
        baseline,
        {},
        ["edit.py", "delete.py", "create.py"],
    )
    scope = capture_task_change_scope(baseline, fingerprints)

    assert isinstance(scope, DatabricksTaskChangeScope)
    assert scope.changed_paths == ("create.py", "delete.py", "edit.py")
    assert scope.provenance["created_paths"] == ("create.py",)
    assert scope.provenance["deleted_paths"] == ("delete.py",)
    assert scope.provenance["content_changes"]["create.py"]["start_sha256"] is None
    assert scope.provenance["content_changes"]["create.py"]["final_sha256"]
    assert scope.provenance["content_changes"]["delete.py"]["start_sha256"]
    assert scope.provenance["content_changes"]["delete.py"]["final_sha256"] is None
    assert (
        scope.provenance["content_changes"]["edit.py"]["start_sha256"]
        != scope.provenance["content_changes"]["edit.py"]["final_sha256"]
    )
    review = task_scope_review_diff(scope)
    assert review["metrics"]["repository_capabilities"]["git_changed_paths_and_diff"] == "unavailable"
    assert review["samples"]["per_file"]["create.py"]["status"] == "created"
    assert review["samples"]["per_file"]["delete.py"]["status"] == "deleted"


@pytest.mark.parametrize("absolute_scope", [
    "/repo/src",
    r"C:\repo\src",
    "C:/repo/src",
    r"\\server\share\src",
])
def test_databricks_scope_rejects_absolute_scope_with_relative_path_guidance(
    tmp_path: Path, absolute_scope: str,
) -> None:
    provider = StubDatabricksRepositoryProvider()

    with pytest.raises(RuntimeError) as exc_info:
        capture_databricks_baseline(tmp_path, provider, [absolute_scope])

    message = str(exc_info.value)
    assert "repository_scope requires relative paths" in message
    assert "['.'] for repository root" in message
    assert "['src'] for src directory" in message
    assert "absolute paths are not supported" in message


def test_databricks_scope_rejects_out_of_scope_acknowledgement(tmp_path: Path) -> None:
    provider = StubDatabricksRepositoryProvider()
    baseline = capture_databricks_baseline(tmp_path, provider, ["source.py"])

    with pytest.raises(RuntimeError, match="outside repository_scope"):
        verify_databricks_task_preconditions(
            baseline,
            {},
            allow_paths=["other.py"],
        )


def test_databricks_scope_rejects_absolute_acknowledgement_with_parent_traversal(
    tmp_path: Path,
) -> None:
    provider = StubDatabricksRepositoryProvider()
    baseline = capture_databricks_baseline(tmp_path, provider, ["."])

    with pytest.raises(RuntimeError, match="escapes repository_scope"):
        verify_databricks_task_preconditions(
            baseline,
            {},
            allow_paths=[str(tmp_path / ".." / "outside.py")],
        )


def test_databricks_scope_rejects_intermediate_directory_symlink(tmp_path: Path) -> None:
    provider = StubDatabricksRepositoryProvider()
    outside = tmp_path.parent / "outside"
    outside.mkdir()
    (outside / "source.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "linked").symlink_to(outside, target_is_directory=True)

    with pytest.raises(RuntimeError, match="repository_scope contains a symlink"):
        capture_databricks_baseline(tmp_path, provider, ["linked/source.py"])


def test_databricks_scope_rechecks_intermediate_symlink_after_acceptance(tmp_path: Path) -> None:
    provider = StubDatabricksRepositoryProvider()
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "source.py").write_text("VALUE = 1\n", encoding="utf-8")
    baseline = capture_databricks_baseline(tmp_path, provider, ["nested/source.py"])
    nested.rename(tmp_path / "preserved-nested")
    nested.symlink_to(tmp_path / "preserved-nested", target_is_directory=True)

    with pytest.raises(RuntimeError, match="authorized scope contains a symlink"):
        capture_task_change_scope(baseline)


def test_databricks_scope_rejects_dangling_symlink(tmp_path: Path) -> None:
    provider = StubDatabricksRepositoryProvider()
    baseline = capture_databricks_baseline(tmp_path, provider, ["source.py"])
    (tmp_path / "source.py").symlink_to("missing.py")

    with pytest.raises(RuntimeError, match="authorized scope contains a symlink"):
        capture_task_change_scope(baseline)


def test_databricks_baseline_rejects_identity_movement_during_capture(
    tmp_path: Path,
) -> None:
    first = StubDatabricksRepositoryProvider().identity
    moved = DatabricksGitFolderIdentity(
        repository_id=first.repository_id,
        workspace_path=first.workspace_path,
        branch="other",
        head_sha="b" * 40,
        remote_url=first.remote_url,
        git_provider=first.git_provider,
    )
    provider = StubDatabricksRepositoryProvider([first, moved])

    with pytest.raises(RuntimeError, match="moved during capture"):
        capture_databricks_baseline(tmp_path, provider, ["source.py"])


def test_databricks_scope_rejects_identity_movement_after_acceptance(
    tmp_path: Path,
) -> None:
    provider = StubDatabricksRepositoryProvider()
    baseline = capture_databricks_baseline(tmp_path, provider, ["source.py"])
    provider.identity = DatabricksGitFolderIdentity(
        repository_id=provider.identity.repository_id,
        workspace_path=provider.identity.workspace_path,
        branch=provider.identity.branch,
        head_sha="b" * 40,
        remote_url=provider.identity.remote_url,
        git_provider=provider.identity.git_provider,
    )

    with pytest.raises(RuntimeError, match="moved after task acceptance"):
        capture_task_change_scope(baseline)
