"""Dispatcher integration for explicit durable task rebinding."""

from __future__ import annotations

import re
import subprocess
from types import SimpleNamespace

import pytest


def test_init_explicitly_rebinds_open_task_without_task_dispatch(tmp_path, monkeypatch):
    anchor_home = tmp_path.parent / f"{tmp_path.name}-anchor-home"
    db = anchor_home / ".agent_memory.db"
    monkeypatch.setenv("ANCHOR_HOME", str(anchor_home))
    monkeypatch.setenv("ANCHOR_MEMORY_DB", str(db))
    from odibi_anchor.bootstrap import init

    anchor, _, _ = init(root=tmp_path, output_format="dict")
    anchor("orient", output_format="dict")
    accepted = anchor(
        "task", "Create restart-safe task authority.",
        goal="Prove explicit rebinding restores the accepted task.",
        mode="documentation", work_type="change", execution_mode="artifact_only",
        risk="low", rigor="direct", continuation=True,
        acceptance_criteria=["The same task window is restored after initialization."],
        output_format="dict",
    )
    window = accepted["accepted_task_authority"]["task_window_id"]

    rebound, _, _ = init(root=tmp_path, rebind_task=True, output_format="dict")
    from odibi_anchor._utils._session_state import _SESSION_STATE

    assert rebound._task_rebind_result["status"] == "rebound"
    assert rebound._task_rebind_result["task_window_id"] == window
    assert rebound._task_rebind_result["obligations"]["acceptance_criteria"] == [
        "The same task window is restored after initialization."
    ]
    assert _SESSION_STATE.task_window_id == window
    assert _SESSION_STATE.active_task_profile.execution_mode == "artifact_only"
    assert _SESSION_STATE.task_goal == "Prove explicit rebinding restores the accepted task."


def test_source_task_rebind_preserves_baseline_after_worktree_becomes_dirty(tmp_path, monkeypatch):
    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=tmp_path, check=True, capture_output=True,
            text=True, encoding="utf-8",
        ).stdout.strip()

    git("init", "-b", "main")
    git("config", "user.email", "tests@example.invalid")
    git("config", "user.name", "Tests")
    git("config", "commit.gpgsign", "false")
    source = tmp_path / "source.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-m", "base")
    anchor_home = tmp_path.parent / f"{tmp_path.name}-anchor-home"
    monkeypatch.setenv("ANCHOR_HOME", str(anchor_home))
    monkeypatch.setenv("ANCHOR_MEMORY_DB", str(anchor_home / ".agent_memory.db"))
    from odibi_anchor.bootstrap import init

    anchor, _, _ = init(root=tmp_path, output_format="dict")
    anchor("orient", output_format="dict")
    accepted = anchor(
        "task", "Change one source file under durable authority.",
        goal="Preserve the clean task-start baseline through process restart.",
        mode="implementation", work_type="change", execution_mode="source_change",
        risk="low", rigor="direct", continuation=True,
        acceptance_criteria=["The dirty diff remains measured from the accepted baseline."],
        output_format="dict",
    )
    source.write_text("VALUE = 2\n", encoding="utf-8")

    rebound, _, _ = init(root=tmp_path, rebind_task=True, output_format="dict")
    from odibi_anchor._repository_snapshot import capture_task_change_scope
    from odibi_anchor._utils._session_state import _SESSION_STATE

    scope = capture_task_change_scope(_SESSION_STATE.task_repository_baseline)
    assert rebound._task_rebind_result["task_window_id"] == accepted["accepted_task_authority"]["task_window_id"]
    assert scope.changed_paths == ("source.py",)
    assert _SESSION_STATE.task_repository_baseline.task_start_head_sha == git("rev-parse", "HEAD")
    qualification = _SESSION_STATE.task_repository_baseline_qualification
    assert qualification.outcome == "unavailable"
    assert qualification.task_window_id == accepted["accepted_task_authority"]["task_window_id"]


def test_ordinary_source_task_still_refuses_a_dirty_initial_worktree(tmp_path, monkeypatch):
    def git(*args):
        subprocess.run(
            ["git", *args], cwd=tmp_path, check=True, capture_output=True,
            text=True, encoding="utf-8",
        )

    git("init", "-b", "main")
    git("config", "user.email", "tests@example.invalid")
    git("config", "user.name", "Tests")
    git("config", "commit.gpgsign", "false")
    source = tmp_path / "source.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-m", "base")
    source.write_text("VALUE = 2\n", encoding="utf-8")
    anchor_home = tmp_path.parent / f"{tmp_path.name}-anchor-home"
    monkeypatch.setenv("ANCHOR_HOME", str(anchor_home))
    monkeypatch.setenv("ANCHOR_MEMORY_DB", str(anchor_home / ".agent_memory.db"))
    from odibi_anchor.bootstrap import init

    anchor, _, _ = init(root=tmp_path, output_format="dict")
    anchor("orient", output_format="dict")

    with pytest.raises(RuntimeError, match="clean initial Git worktree"):
        anchor(
            "task", "Do not absorb pre-existing source changes.",
            goal="Keep ordinary task acceptance fail-closed.",
            mode="implementation", work_type="change", execution_mode="source_change",
            risk="low", rigor="direct", continuation=True,
            acceptance_criteria=["Dirty initial worktrees remain blocked."],
            output_format="dict",
        )


def test_init_rebind_reports_unavailable_instead_of_reconstructing(tmp_path, monkeypatch):
    anchor_home = tmp_path / "anchor-home"
    monkeypatch.setenv("ANCHOR_HOME", str(anchor_home))
    monkeypatch.setenv("ANCHOR_MEMORY_DB", str(anchor_home / ".agent_memory.db"))
    from odibi_anchor.bootstrap import init
    with pytest.raises(RuntimeError, match="unavailable"):
        init(root=tmp_path, rebind_task=True, output_format="dict")


def test_mcp_visible_action_rebinds_without_reinitializing_dispatcher(tmp_path, monkeypatch):
    anchor_home = tmp_path.parent / f"{tmp_path.name}-anchor-home"
    monkeypatch.setenv("ANCHOR_HOME", str(anchor_home))
    monkeypatch.setenv("ANCHOR_MEMORY_DB", str(anchor_home / ".agent_memory.db"))
    from odibi_anchor.bootstrap import init

    anchor, _, _ = init(root=tmp_path, output_format="dict")
    anchor("orient", output_format="dict")
    accepted = anchor(
        "task", "Retain authority for an MCP action.",
        goal="Allow the active host to rebind without changing server configuration.",
        mode="documentation", execution_mode="artifact_only", risk="low", rigor="direct",
        continuation=True, acceptance_criteria=["task_rebind restores this window."],
        output_format="dict",
    )
    window = accepted["accepted_task_authority"]["task_window_id"]

    current, _, _ = init(root=tmp_path, output_format="dict")
    rebound = current("task_rebind", output_format="dict")

    assert rebound["kind"] == "task_authority_rebind"
    assert rebound["task_window_id"] == window
    assert rebound["diagnostics"]["rebindings"] == 1
    assert rebound["obligations"]["acceptance_criteria"] == [
        "task_rebind restores this window."
    ]
    current("status", output_format="dict")
    current("skill_loaded", "documentation", output_format="dict")
    current("touched", "rebound-note.md", output_format="dict")
    from odibi_anchor._utils._session_state import (
        _SESSION_FILES_CHANGED,
        _SESSION_STATE,
        _SESSION_TIMINGS,
    )

    assert len(_SESSION_FILES_CHANGED) == 1
    assert "rebound-note.md" in _SESSION_FILES_CHANGED
    assert _SESSION_STATE.task_verification_epoch == next(
        index for index, timing in enumerate(_SESSION_TIMINGS, start=1)
        if timing.get("synthetic") == "durable_rebind"
    )
    assert any(
        timing["action"] == "task"
        and timing.get("synthetic") == "durable_rebind"
        and timing["passed"] is True
        for timing in _SESSION_TIMINGS
    )


def test_mcp_visible_approval_adopts_exact_dirty_continuation(tmp_path, monkeypatch):
    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=tmp_path, check=True, capture_output=True,
            text=True, encoding="utf-8",
        ).stdout.strip()

    git("init", "-b", "main")
    git("config", "user.email", "tests@example.invalid")
    git("config", "user.name", "Tests")
    git("config", "commit.gpgsign", "false")
    source = tmp_path / "source.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-m", "base")
    anchor_home = tmp_path.parent / f"{tmp_path.name}-anchor-home"
    monkeypatch.setenv("ANCHOR_HOME", str(anchor_home))
    monkeypatch.setenv("ANCHOR_MEMORY_DB", str(anchor_home / ".agent_memory.db"))
    monkeypatch.setenv("ANCHOR_TRUST_DOMAIN", "private")
    monkeypatch.setenv("ANCHOR_SLACK_USER_ID", "owner-1")
    from odibi_anchor.bootstrap import init

    anchor, _, _ = init(root=tmp_path, output_format="dict")
    anchor("orient", output_format="dict")
    prior = anchor(
        "task", "Change a source file under durable authority.",
        goal="Retain the original diff across adopted continuation.",
        mode="implementation", work_type="change", execution_mode="source_change",
        risk="low", rigor="direct", continuation=True,
        repository_scope=["source.py"],
        acceptance_criteria=["The complete source.py diff remains governed."],
        output_format="dict",
    )
    source.write_text("VALUE = 2\n", encoding="utf-8")

    def approve(message, **_kwargs):
        challenge = re.search(r"APPROVE ([0-9a-f]{64})", message).group(1)
        return SimpleNamespace(
            request_id="request-1", response_message_id="reply-1",
            response=f"APPROVE {challenge}", response_user_id="owner-1",
            transport="authenticated-test", transport_ref="private-reference",
            message=message, created_at=1.0, delivered_at=1.1, responded_at=1.2,
        )

    monkeypatch.setattr("odibi_anchor.human_input.request_human_input_record", approve)
    approval = anchor(
        "task_adoption", "request",
        prior_task_window_id=prior["accepted_task_authority"]["task_window_id"],
        timeout_minutes=1, output_format="dict",
    )
    restarted, _, _ = init(root=tmp_path, output_format="dict")
    restarted("orient", output_format="dict")
    adopted = restarted(
        "task", "Continue the exact previously accepted source change.",
        goal="Restore gate reachability without mutating the worktree.",
        mode="implementation", work_type="change", execution_mode="source_change",
        risk="low", rigor="direct", continuation=True,
        repository_scope=["source.py"],
        adoption_approval_id=approval["approval_id"],
        acceptance_criteria=["The complete source.py diff remains governed."],
        output_format="dict",
    )
    from odibi_anchor._repository_snapshot import capture_task_change_scope
    from odibi_anchor._utils._session_state import _SESSION_STATE

    assert adopted["repository_evidence"]["authority_kind"] == "adopted"
    assert adopted["accepted_task_authority"]["adoption"]["approval_id"] == approval["approval_id"]
    assert _SESSION_STATE.task_repository_baseline.authority_kind == "adopted"
    assert capture_task_change_scope(_SESSION_STATE.task_repository_baseline).changed_paths == (
        "source.py",
    )
    reviewed = restarted("review", output_format="dict")
    assert reviewed["metrics"]["baseline_authority"] == "adopted"
    assert reviewed["samples"]["adoption_provenance"]["approval_id"] == approval["approval_id"]
    assert set(reviewed["samples"]["per_file"]) == {"source.py"}
    assert any("complete original diff" in item for item in reviewed["findings"])
    with pytest.raises(RuntimeError, match="recoverable prior authority"):
        restarted(
            "task_adoption", "request",
            prior_task_window_id=prior["accepted_task_authority"]["task_window_id"],
            timeout_minutes=1, output_format="dict",
        )
    diagnostics = restarted("task_adoption", "inspect", output_format="dict")
    assert diagnostics["approvals"] == 1
    assert diagnostics["adoptions"] == 1
    assert diagnostics["durable_windows"] == 2
    assert diagnostics["rebindings"] == 0
    assert diagnostics["refused_adoptions"] == 1
    assert diagnostics["refusals_by_precondition"] == {"recoverable prior authority": 1}
    withdrawn = restarted(
        "task_adoption", "withdraw",
        adoption_id=adopted["accepted_task_authority"]["adoption"]["adoption_id"],
        reason="integration-test withdrawal", output_format="dict",
    )
    assert withdrawn["kind"] == "task_adoption_withdrawal"
    assert restarted("task_adoption", "inspect", output_format="dict")["active_adoptions"] == 0
    with pytest.raises(RuntimeError, match="adopted task authority was withdrawn"):
        restarted("gate", output_format="dict")
