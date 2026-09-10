"""Causal task evidence at the managed gate boundary."""

from __future__ import annotations

import subprocess
from dataclasses import replace
from types import SimpleNamespace

import pytest

from odibi_anchor._dispatcher._gate_wrappers import gate_with_auto_confirm


def _timing(action: str, *, error=None, passed=True):
    return {"action": action, "elapsed_ms": 1, "error": error, "passed": passed}


def _run_gate(
    tmp_path,
    timings,
    epoch,
    *,
    deferred=False,
    task_baseline=None,
    task_write_fingerprints=None,
):
    state = SimpleNamespace(
        active_spec=None,
        active_task_mode="implementation",
        checkpoint_in_progress=({"label": "nested"} if deferred else None),
        files_at_last_checkpoint=7,
        notebook_path=None,
        skill_hints_emitted=set(),
        skills_loaded=set(),
        task_repository_baseline=task_baseline,
        task_repository_write_fingerprints=dict(task_write_fingerprints or {}),
        task_verification_epoch=epoch,
    )

    def workflow_gate(_root, *_args, **kwargs):
        paid = list(kwargs["obligations_paid"])
        return {
            "actions_taken": list(kwargs["actions_taken"]),
            "metrics": {"risk_level": "low", "timing_verified": paid},
            "obligations": [],
            "obligations_paid": [
                {"tool": tool, "paid_by": "timing_verified"} for tool in paid
            ],
        }

    result = gate_with_auto_confirm(
        tmp_path,
        (),
        {},
        session_timings=timings,
        session_files_changed=set(),
        session_files_created=set(),
        session_frame=None,
        frame_enabled=False,
        session_state=state,
        workflow_gate_fn=workflow_gate,
        check_drift_fn=lambda _root: {"has_drift": False},
        check_test_coverage_fn=lambda **_kwargs: True,
    )
    return result, state


def _paid_tools(result):
    return {entry["tool"] for entry in result["obligations_paid"]}


def test_exact_successful_task_predecessor_pays_planning(tmp_path):
    result, _ = _run_gate(
        tmp_path,
        [_timing("task"), _timing("safe")],
        1,
    )

    assert "task" not in result["actions_taken"]
    assert "task_execution_context" in result["metrics"]["timing_verified"]
    assert {
        "tool": "task_execution_context",
        "paid_by": "timing_verified",
    } in result["obligations_paid"]


def test_databricks_gate_reports_limited_capabilities_and_manual_delivery(tmp_path):
    from odibi_anchor._repository_snapshot import (
        DatabricksGitFolderIdentity,
        acknowledge_databricks_task_writes,
        capture_databricks_git_folder_task_baseline,
    )

    identity = DatabricksGitFolderIdentity(
        "42",
        "/Users/test@example.invalid/odibi_anchor",
        "main",
        "a" * 40,
        "https://example.invalid/repository.git",
        "gitHub",
    )

    class Provider:
        provider_id = "test.databricks-repos"

        def capture_identity(self, _target):
            return identity

    source = tmp_path / "source.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    baseline = capture_databricks_git_folder_task_baseline(
        tmp_path,
        Provider(),
        ["source.py"],
        accept_unknown_git_state=True,
    )
    source.write_text("VALUE = 2\n", encoding="utf-8")
    fingerprints = acknowledge_databricks_task_writes(baseline, {}, ["source.py"])

    result, _ = _run_gate(
        tmp_path,
        [_timing("task"), _timing("preflight"), _timing("test"), _timing("review")],
        1,
        task_baseline=baseline,
        task_write_fingerprints=fingerprints,
    )

    capabilities = result["metrics"]["repository_capabilities"]
    assert result["metrics"]["scope_source"] == "databricks_git_folder"
    assert result["metrics"]["host_identity_stability"] == "verified"
    assert capabilities["host_repository_identity"] == "available"
    assert capabilities["local_worktree_status"] == "unavailable"
    assert capabilities["git_changed_paths_and_diff"] == "unavailable"
    assert capabilities["merge_base_and_history"] == "unavailable"
    assert capabilities["pr_readiness"] == "unavailable"
    content = result["samples"]["task_scoped_content_changes"]["source.py"]
    assert result["metrics"]["task_scoped_content_change_count"] == 1
    assert content["status"] == "modified"
    assert content["start_sha256"] != content["final_sha256"]
    assert any("complete Databricks Repos UI diff" in risk for risk in result["risks"])
    assert any("Commit and push manually" in action for action in result["suggested_next_actions"])


def test_gate_surfaces_adopted_authority_and_complete_original_diff(tmp_path):
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
    from odibi_anchor._repository_snapshot import capture_task_repository_baseline

    baseline = capture_task_repository_baseline(tmp_path, "main")
    provenance = {
        "authority_kind": "adopted", "approval_id": "ada-test",
        "challenge_sha256": "a" * 64, "prior_task_window_id": "ltw-prior",
        "changed_paths": ["source.py"], "index_fingerprint": "b" * 64,
        "worktree_fingerprint": "c" * 64,
    }
    baseline = replace(
        baseline, authority_kind="adopted", adoption_provenance=provenance,
    )
    source.write_text("VALUE = 2\n", encoding="utf-8")

    result, _ = _run_gate(
        tmp_path,
        [_timing("task"), _timing("preflight"), _timing("test"), _timing("review")],
        1, task_baseline=baseline,
    )

    assert result["metrics"]["baseline_authority"] == "adopted"
    retained = result["samples"]["adoption_provenance"]
    assert retained["approval_id"] == "ada-test"
    assert tuple(retained["changed_paths"]) == ("source.py",)
    assert retained["worktree_fingerprint"] == "c" * 64
    assert any("complete original diff" in item for item in result["findings"])


@pytest.mark.parametrize(
    ("epoch", "timings"),
    [
        pytest.param(2, [_timing("task"), _timing("status")], id="non-adjacent"),
        pytest.param(1, [_timing("status")], id="mismatched-action"),
        pytest.param(1, [_timing("task", error="RuntimeError")], id="errored"),
        pytest.param(1, [_timing("task", passed=False)], id="failed"),
        pytest.param(1, [{"action": "task", "error": None}], id="missing-passed"),
        pytest.param(1, [_timing("task", passed=1)], id="truthy-int-passed"),
        pytest.param(1, [_timing("task", passed="yes")], id="truthy-string-passed"),
    ],
)
def test_inapplicable_predecessor_does_not_pay_planning(tmp_path, epoch, timings):
    result, _ = _run_gate(tmp_path, timings, epoch)

    assert "task_execution_context" not in _paid_tools(result)


@pytest.mark.parametrize("epoch", [None, False, True, 0, -1, 2])
def test_invalid_epoch_fails_closed(tmp_path, epoch):
    result, _ = _run_gate(tmp_path, [_timing("task")], epoch)

    assert "task_execution_context" not in _paid_tools(result)


def test_post_epoch_task_cannot_pay_planning(tmp_path):
    result, _ = _run_gate(
        tmp_path,
        [_timing("status"), _timing("task")],
        1,
    )

    assert "task" in result["actions_taken"]
    assert "task_execution_context" not in _paid_tools(result)


def test_failed_replacement_does_not_replace_accepted_task_payment(tmp_path):
    result, _ = _run_gate(
        tmp_path,
        [_timing("task"), _timing("safe"), _timing("task", passed=False)],
        1,
    )

    assert "task" in result["actions_taken"]
    assert [
        entry for entry in result["obligations_paid"]
        if entry["tool"] == "task_execution_context"
    ] == [{"tool": "task_execution_context", "paid_by": "timing_verified"}]


def test_implementation_verification_remains_post_task(tmp_path):
    result, _ = _run_gate(
        tmp_path,
        [
            _timing("preflight"),
            _timing("test"),
            _timing("task"),
            _timing("consistency"),
        ],
        3,
    )

    assert _paid_tools(result) == {
        "consistency_check_context",
        "task_execution_context",
    }
    assert result["actions_taken"] == ["consistency"]


def test_deferred_gate_uses_same_task_evidence_without_committing_state(tmp_path):
    result, state = _run_gate(tmp_path, [_timing("task")], 1, deferred=True)

    assert "task_execution_context" in _paid_tools(result)
    assert state.files_at_last_checkpoint == 7


def _git(root, *args):
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    )


def test_public_task_before_change_lifecycle_pays_planning(tmp_path, monkeypatch):
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    (project / "tests").mkdir()
    (project / "src" / "example.py").write_text("VALUE = 1\n", encoding="utf-8")
    (project / "tests" / "test_example.py").write_text(
        "def test_example():\n    assert True\n", encoding="utf-8"
    )
    _git(project, "init", "-b", "main")
    _git(project, "config", "user.email", "test@example.com")
    _git(project, "config", "user.name", "Test User")
    _git(project, "config", "commit.gpgsign", "false")
    _git(project, "add", ".")
    _git(project, "commit", "-m", "baseline")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    from odibi_anchor.bootstrap import init

    anchor, _root, _manifest = init(root=str(project), output_format="dict")
    from odibi_anchor._utils._session_state import _SESSION_STATE

    # The package checkout may have delivery debt from the outer test runner.
    # Keep that persisted harness state out of this isolated target lifecycle.
    _SESSION_STATE.prior_learn_debt = False
    anchor("status")
    anchor("memory")
    anchor("audit_history")
    anchor("new_session", name="task-before-change")
    task = anchor(
        "task",
        "Add a source import through the public lifecycle",
        goal="Verify accepted task accounting at gate",
        mode="implementation",
        work_type="change",
        execution_mode="source_change",
        risk="low",
        rigor="direct",
        known_facts=["The repository has one committed source module."],
        constraints=["Use the public dispatcher for the source edit and verification."],
        acceptance_criteria=["Gate reports task_execution_context as timing-verified."],
        in_scope=["src/example.py"],
        out_of_scope=["Public API signatures"],
        background="The accepted task timing precedes the source verification epoch.",
        deliverables=["One verified source edit."],
        output_format="dict",
    )
    for required in task["required_skills"]:
        if required["path"] != "n/a":
            anchor("skill_loaded", required["skill"])
    anchor("known_bad", changed_files=["src/example.py"], output_format="dict")
    changed = anchor(
        "safe",
        target="src/example.py",
        action="add_import",
        import_statement="from __future__ import annotations",
        apply=True,
        verify=False,
        test=False,
        output_format="dict",
    )
    assert changed["metrics"]["applied"] is True
    anchor("review", output_format="dict")
    anchor("preflight", output_format="dict")
    anchor("test", target="tests", output_format="dict")
    gate = anchor("gate", output_format="dict")
    anchor(
        "learn",
        session_events=[{
            "type": "discovery",
            "detail": "The accepted task paid planning through exact timing provenance.",
        }],
        output_format="dict",
    )

    assert not any(
        obligation.get("created_by_action") == "missing_planning"
        for obligation in gate["obligations"]
    )
    assert "task_execution_context" in gate["metrics"]["timing_verified"]
    assert {
        "tool": "task_execution_context",
        "paid_by": "timing_verified",
    } in gate["obligations_paid"]
