"""Artifact-only modes may deliver managed records; read-only modes may not.

Issue #13: the gate mismatch check named `analysis`, `review`, `retrospective`,
`planning` and `decision` as read-only. Three of those declare `artifact_only`, so
modes whose purpose is writing managed records were rejected at gate for writing
them — and with `implementation` requiring a clean Git worktree, a non-Git artifact
root had no mode that could both write and gate.

The source-edit boundary is `task_profile_effect_compatible`, which refuses the
`source_write` effect unless the execution mode is `source_change`. This check only
has to reject `read_only`.
"""
import pytest

from odibi_anchor._dispatcher._effects import task_profile_effect_compatible
from odibi_anchor._dispatcher._enforcement import should_block_mode_mismatch
from odibi_anchor.planning._task_profile import _LEGACY_DEFAULTS, normalize_task_profile

CHANGED = {"work_items/WI-2026-0001.md"}
ARTIFACT_ROOT = "/tmp/managed-project"

ARTIFACT_ONLY_MODES = sorted(
    name for name, defaults in _LEGACY_DEFAULTS.items() if defaults[1] == "artifact_only"
)
READ_ONLY_MODES = sorted(
    name for name, defaults in _LEGACY_DEFAULTS.items() if defaults[1] == "read_only"
)


def _timings(mode):
    return [{"action": "task", "error": None, "task_mode": mode}]


def _check(mode, paths=CHANGED):
    return should_block_mode_mismatch(
        _timings(mode), paths,
        artifact_root=ARTIFACT_ROOT, target_root=ARTIFACT_ROOT,
    )


def test_the_two_mode_families_are_both_populated():
    """Guard the premise: the parametrised cases below would be vacuous otherwise."""
    assert ARTIFACT_ONLY_MODES and READ_ONLY_MODES
    assert not set(ARTIFACT_ONLY_MODES) & set(READ_ONLY_MODES)


@pytest.mark.parametrize("mode", ARTIFACT_ONLY_MODES)
def test_artifact_only_modes_may_deliver_managed_records(mode):
    blocked, message = _check(mode)
    assert blocked is False, f"{mode} is artifact_only but was blocked: {message}"


@pytest.mark.parametrize("mode", READ_ONLY_MODES)
def test_read_only_modes_are_still_rejected(mode):
    blocked, message = _check(mode)
    assert blocked is True, f"{mode} is read_only and must not deliver file changes"
    assert mode in message


def test_planning_specifically_is_no_longer_blocked():
    """The mode the issue reported. It declares artifact_only."""
    assert _LEGACY_DEFAULTS["planning"][1] == "artifact_only"
    blocked, _ = _check("planning")
    assert blocked is False


def test_analysis_specifically_is_still_blocked():
    assert _LEGACY_DEFAULTS["analysis"][1] == "read_only"
    blocked, _ = _check("analysis")
    assert blocked is True


def test_rejection_message_names_a_mode_that_can_actually_write():
    """The old message sent callers to `implementation`, which needs a clean worktree."""
    _, message = _check("analysis")
    assert "artifact_only" in message
    assert "{planned_mode}" not in message, "placeholder must be interpolated"


def test_no_files_changed_is_never_a_mismatch():
    for mode in ARTIFACT_ONLY_MODES + READ_ONLY_MODES:
        blocked, _ = should_block_mode_mismatch(_timings(mode), set())
        assert blocked is False


def test_source_and_data_modes_are_unaffected():
    """Negative control: this check never governed the writing modes."""
    for mode in ("implementation", "migration", "data", "etl", "refresh"):
        blocked, _ = should_block_mode_mismatch(_timings(mode), CHANGED)
        assert blocked is False


@pytest.mark.parametrize("mode", [mode for mode in ARTIFACT_ONLY_MODES if mode != "documentation"])
def test_artifact_only_modes_reject_target_root_drift(mode):
    blocked, message = _check(mode, {"src/package.py"})
    assert blocked is True
    assert "Non-artifact files" in message


def test_documentation_mode_retains_its_markdown_specific_policy():
    blocked, _ = _check("documentation", {"README.md"})
    assert blocked is False


# ── the boundary this check is not responsible for ───────────────────────────


@pytest.mark.parametrize("mode", ARTIFACT_ONLY_MODES)
def test_artifact_only_modes_still_cannot_write_source(mode):
    """Relaxing the name list must not open a path to source edits."""
    profile = normalize_task_profile(legacy_mode=mode)
    assert profile.execution_mode == "artifact_only"
    assert task_profile_effect_compatible("source_write", profile) is False
    assert task_profile_effect_compatible("artifact_write", profile) is True


@pytest.mark.parametrize("mode", READ_ONLY_MODES)
def test_read_only_modes_cannot_write_anything(mode):
    profile = normalize_task_profile(legacy_mode=mode)
    assert task_profile_effect_compatible("artifact_write", profile) is False
    assert task_profile_effect_compatible("source_write", profile) is False
