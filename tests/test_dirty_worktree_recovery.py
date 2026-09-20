"""The dirty-worktree block must be self-guiding without relaxing the baseline rule.

Issue #6: `capture_task_repository_baseline` correctly fails closed when a
source-change task starts on a dirty worktree, but raised a bare RuntimeError. The
caller could not tell whether the intended work was artifact-only, belonged to an
interrupted task, or was blocked behind changes another task already owns.
"""
import subprocess

import pytest

from odibi_anchor._repository_snapshot import capture_task_repository_baseline

# Pinned as a literal, not imported: this is the public message callers and docs
# already match on, so it must not drift even if the internal constant is renamed.
DIRTY_WORKTREE_MESSAGE = "BLOCKED: source-change task requires a clean initial Git worktree"

FORBIDDEN_RECOVERIES = ("stash", "discard", "checkout", "reset", "clean", "commit")


def _git(root, *args):
    subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True,
    )


@pytest.fixture
def dirty_repo(tmp_path):
    """A born repository on `main` with one staged, one unstaged and one untracked path."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "Test")
    (root / "tracked.txt").write_text("base\n")
    (root / "staged.txt").write_text("base\n")
    _git(root, "add", "tracked.txt", "staged.txt")
    _git(root, "commit", "-m", "base")

    (root / "staged.txt").write_text("staged change\n")
    _git(root, "add", "staged.txt")
    (root / "tracked.txt").write_text("unstaged change\n")
    (root / "untracked.txt").write_text("new\n")
    return root


@pytest.fixture
def dirty_error(dirty_repo):
    with pytest.raises(RuntimeError) as excinfo:
        capture_task_repository_baseline(dirty_repo, "main")
    return excinfo.value


# ── the baseline rule and its public contract are unchanged ──────────────────


def test_still_fails_closed_with_the_same_class_and_message(dirty_error):
    assert type(dirty_error) is RuntimeError
    assert str(dirty_error) == DIRTY_WORKTREE_MESSAGE


def test_clean_worktree_still_succeeds(dirty_repo):
    """Negative control: the recovery metadata must not change when a baseline is valid."""
    _git(dirty_repo, "add", "-A")
    _git(dirty_repo, "commit", "-m", "clean up")
    baseline = capture_task_repository_baseline(dirty_repo, "main")
    assert baseline.branch == "main"


# ── bounded, machine-readable context ────────────────────────────────────────


def test_exposes_a_stable_error_code(dirty_error):
    assert dirty_error.error_code == "source_change_requires_clean_worktree"


def test_context_counts_dirty_paths_by_category(dirty_error):
    context = dirty_error.context
    assert context["staged_path_count"] == 1
    assert context["unstaged_path_count"] == 1
    assert context["untracked_path_count"] == 1
    assert context["dirty_path_count"] == 3


def test_context_reports_the_requested_execution_intent(dirty_error):
    assert dirty_error.context["requested_execution_mode"] == "source_change"
    assert dirty_error.context["branch"] == "main"


def test_context_is_bounded_and_carries_no_path_contents(dirty_error):
    """Counts, not payloads: the block must not leak worktree contents into evidence."""
    rendered = repr(dirty_error.context)
    assert "unstaged change" not in rendered
    assert "staged change" not in rendered


# ── recovery routes ──────────────────────────────────────────────────────────


def test_offers_only_executable_recovery_routes(dirty_error):
    actions = [operation["action"] for operation in dirty_error.next_operations]
    assert actions == ["task_rebind", "task"]


def test_artifact_only_route_does_not_claim_source_change_authority(dirty_error):
    task_route = next(
        operation for operation in dirty_error.next_operations
        if operation["action"] == "task"
    )
    assert task_route["kwargs"]["mode"] == "planning"


def test_every_route_is_copy_ready(dirty_error):
    for operation in dirty_error.next_operations:
        assert operation["copy_ready"].startswith("anchor(")
        assert operation["reason"]


def test_next_operation_mirrors_the_first_route(dirty_error):
    assert dirty_error.next_operation == dirty_error.next_operations[0]


# ── the routes must never absorb changes of unknown ownership ────────────────


def test_no_route_suggests_destroying_or_absorbing_existing_changes(dirty_error):
    """Scoped to the routes: these are what an agent executes.

    The context prose is checked separately — it names these operations in order to
    forbid them, which is the opposite of directing an agent to run one.
    """
    rendered = repr(dirty_error.next_operations)
    lowered = rendered.lower()
    for forbidden in FORBIDDEN_RECOVERIES:
        assert forbidden not in lowered, f"a recovery route mentions {forbidden!r}: {rendered}"


def test_context_explicitly_forbids_absorbing_the_changes(dirty_error):
    authority = dirty_error.context["resolution_authority"].lower()
    assert "never" in authority
    assert "stash" in authority and "discard" in authority


def test_continuation_is_not_offered(dirty_error):
    """`continuation=True` would meet the same dirty baseline and fail identically."""
    for operation in dirty_error.next_operations:
        assert "continuation" not in operation["kwargs"]


def test_context_states_that_ownership_must_be_resolved_first(dirty_error):
    assert "owner" in dirty_error.context["resolution_authority"].lower()
