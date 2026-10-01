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
import subprocess
import sys
import textwrap
from pathlib import Path

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


@pytest.mark.parametrize("mode", ARTIFACT_ONLY_MODES)
def test_artifact_only_modes_reject_target_root_drift(mode):
    blocked, message = _check(mode, {"src/package.py"})
    assert blocked is True
    assert "Non-artifact files" in message


def test_documentation_markdown_is_not_automatically_a_managed_artifact():
    blocked, _ = _check("documentation", {"README.md"})
    assert blocked is True


@pytest.mark.parametrize("mode", ["implementation", "analysis", "documentation"])
@pytest.mark.parametrize("execution", ["artifact_only", "read_only", "source_change"])
def test_accepted_profile_overrides_legacy_defaults(mode, execution):
    blocked, _ = should_block_mode_mismatch(
        _timings(mode), CHANGED, execution_mode=execution,
        artifact_root=ARTIFACT_ROOT, target_root=ARTIFACT_ROOT,
    )
    assert blocked is (execution == "read_only")


def test_artifact_boundary_resolves_paths_and_symlinks(tmp_path):
    artifact = tmp_path / "artifacts"
    target = tmp_path / "target"
    (artifact / "work_items").mkdir(parents=True)
    target.mkdir()
    (artifact / "work_items" / "escape").symlink_to(target, target_is_directory=True)
    for path, expected in [
        (artifact / "work_items" / "valid.md", False),
        (target / "work_items" / "fake.md", True),
        (artifact / "work_items" / "escape" / "source.md", True),
        (artifact / "work_items" / ".." / "source.md", True),
    ]:
        blocked, _ = should_block_mode_mismatch(
            _timings("implementation"), {str(path)}, execution_mode="artifact_only",
            artifact_root=str(artifact), target_root=str(target),
        )
        assert blocked is expected, path


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


@pytest.mark.parametrize("execution_mode", ["artifact_only", "read_only"])
@pytest.mark.parametrize("registered", [False, True])
def test_public_gate_rejects_explicit_profile_target_drift(tmp_path, execution_mode, registered):
    from odibi_anchor.pytest_runner import child_environment

    script = textwrap.dedent('''
        import sys
        from pathlib import Path
        from odibi_anchor.startup import launch, register_project
        base = Path(sys.argv[1])
        target = base / "target"
        target.mkdir()
        path = target / "config.json"
        path.write_text('{"value":1}\\n')
        register_project(anchor_home=base / "home", project_id="probe", project_root=target)
        anchor = launch(anchor_home=base / "home", project_id="probe", project_root=target)
        orientation = anchor("orient", output_format="dict")
        session = anchor("new_session", name="boundary", inline=True, output_format="dict")
        task = anchor("task", "Verify explicit execution boundaries", goal="Reject target drift",
                      mode="implementation", execution_mode=sys.argv[2],
                      acceptance_criteria=["Unauthorized target changes cannot pass"], output_format="dict")
        skill = anchor("skill_loaded", "code-comprehension", output_format="dict")
        path.write_text('{"value":2}\\n')
        if sys.argv[3] == "True":
            try:
                touched = anchor("touched", "config.json", output_format="dict")
            except RuntimeError as error:
                assert "incompatible" in str(error)
        review = anchor("review", output_format="dict")
        try:
            gate = anchor("gate", output_format="dict")
        except RuntimeError as error:
            assert "Mode mismatch" in str(error), str(error)
        else:
            raise AssertionError("Unauthorized target drift passed gate")
    ''')
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path), execution_mode, str(registered)],
        env={**child_environment(), "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")},
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
