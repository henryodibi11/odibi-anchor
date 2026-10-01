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
    assert dirty_error.context["ownership_state"] == "unavailable"


def test_context_is_bounded_and_carries_no_path_contents(dirty_error):
    """Counts, not payloads: the block must not leak worktree contents into evidence."""
    rendered = repr(dirty_error.context)
    assert "unstaged change" not in rendered
    assert "staged change" not in rendered


# ── recovery routes ──────────────────────────────────────────────────────────


def test_offers_only_executable_recovery_routes(dirty_error):
    actions = [operation["action"] for operation in dirty_error.next_operations]
    assert actions == ["task"]


def test_interrupted_task_recovery_offers_exact_rebind_before_artifact_route(dirty_repo):
    with pytest.raises(RuntimeError) as excinfo:
        capture_task_repository_baseline(
            dirty_repo,
            "main",
            task_authority_context={
                "ownership_state": "interrupted_source_task",
                "matching_open_source_task_count": 1,
                "matching_open_source_task_ids": ["ltw-owned"],
                "matching_terminal_source_task_count": 0,
                "matching_terminal_source_task_ids": [],
            },
        )

    error = excinfo.value
    assert error.context["ownership_state"] == "interrupted_source_task"
    assert error.next_operations[0]["action"] == "task_rebind"
    assert error.next_operations[0]["kwargs"] == {"task_window_id": "ltw-owned"}


def test_completed_task_recovery_does_not_offer_invalid_rebind(dirty_repo):
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=dirty_repo, check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    with pytest.raises(RuntimeError) as excinfo:
        capture_task_repository_baseline(
            dirty_repo,
            "main",
            task_authority_context={
                "ownership_state": "terminal_source_task_candidates",
                "matching_open_source_task_count": 0,
                "matching_open_source_task_ids": [],
                "matching_terminal_source_task_count": 1,
                "matching_terminal_source_task_ids": ["ltw-completed"],
                "_terminal_source_candidates": [{
                    "task_window_id": "ltw-completed",
                    "branch": "main",
                    "changed_paths": ("staged.txt", "tracked.txt", "untracked.txt"),
                    "end_revision": head,
                }],
            },
        )

    error = excinfo.value
    assert error.context["ownership_state"] == "completed_task_delivery"
    assert error.context["source_change_recovery"] == (
        "The matching source task is terminal. Resolve or complete delivery under that "
        "task's retained authority; do not create a new task that absorbs its changes."
    )
    assert [item["action"] for item in error.next_operations] == ["task"]


def test_historical_terminal_task_without_exact_diff_match_is_not_claimed(dirty_repo):
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=dirty_repo, check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    with pytest.raises(RuntimeError) as excinfo:
        capture_task_repository_baseline(
            dirty_repo,
            "main",
            task_authority_context={
                "ownership_state": "terminal_source_task_candidates",
                "matching_open_source_task_count": 0,
                "matching_open_source_task_ids": [],
                "matching_terminal_source_task_count": 1,
                "matching_terminal_source_task_ids": ["ltw-historical"],
                "_terminal_source_candidates": [{
                    "task_window_id": "ltw-historical",
                    "branch": "main",
                    "changed_paths": ("different.py",),
                    "end_revision": head,
                }],
            },
        )

    error = excinfo.value
    assert error.context["ownership_state"] == "unowned_or_ambiguous"
    assert error.context["matching_terminal_source_task_count"] == 0
    assert "_terminal_source_candidates" not in error.context
    assert [item["action"] for item in error.next_operations] == ["task"]


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
