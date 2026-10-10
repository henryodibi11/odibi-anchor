"""End-to-end replay of the v0.3.24 self-hosting lifecycle friction (issue #39).

One high-risk workflow candidate with more than 15 planned files is driven through
the public dispatcher from inquiry to ``qualify``: Spec requirement surfaced at
acceptance, review-before-execute Spec linking, uncapped planned registrations, a
dispatcher restart with byte-bound continuity, measurement, producer closure, a
separate read-only review task, and qualification. On v0.3.24 the 16th ``touched``
is blocked by the per-checkpoint cap whose remedy would close the producer.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

PLANNED = [f"pkg/module_{index:02d}.py" for index in range(17)]
SPEC = """---
status: ready
mode: implementation
permissions_needed: []
files_touched:
  - pkg/module_00.py
---
# Replay Spec

## Problem
Seventeen modules carry the wrong constant.

## Design
Change every planned module; verify with anchor("test") and anchor("gate"). Applies writing-specs.

## Acceptance Criteria
- WHEN any planned module is imported, VALUE SHALL equal 2.

## Verification
Measure the replay criterion through the workflow.

## Risks
None beyond the disposable replay target.
"""


def _git(target: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=target, check=True, capture_output=True, text=True).stdout.strip()


def _advance(anchor, command, **kwargs):
    current = anchor("workflow", output_format="dict")["state"]
    return anchor("workflow", command, expected_generation=current["generation"],
                  request_id=f"{command}:{current['generation']}", output_format="dict", **kwargs)["state"]


def test_high_risk_candidate_with_more_than_fifteen_planned_files_reaches_qualify(tmp_path, monkeypatch):
    from odibi_anchor._dispatcher._project import project_action
    from odibi_anchor.bootstrap import init

    home, target = tmp_path / "home", tmp_path / "target"
    home.mkdir()
    (target / "pkg").mkdir(parents=True)
    for path in PLANNED:
        (target / path).write_text("VALUE = 1\n")
    (target / "test_replay.py").write_text(
        "import importlib\n\n"
        f"def test_all_modules_changed():\n    for name in {[p[:-3].replace('/', '.') for p in PLANNED]!r}:\n"
        "        assert importlib.import_module(name).VALUE == 2\n"
    )
    (target / "pkg" / "__init__.py").write_text("")
    _git(target, "init", "-b", "main")
    _git(target, "add", ".")
    _git(target, "commit", "-m", "base")
    monkeypatch.setenv("ANCHOR_HOME", str(home))
    monkeypatch.setenv("ANCHOR_MEMORY_DB", str(home / "memory.db"))
    monkeypatch.setenv("ANCHOR_TRUST_DOMAIN", "work")
    project_action(home, "create", name="replay", target=target, output_format="dict")
    anchor, _, _ = init(root=str(target), project="replay", output_format="dict")

    # Inquiry drafts the plan; closing it needs no failure notes.
    anchor("orient", output_format="dict")
    anchor("new_session", name="replay_inquiry", inline=True, output_format="dict")
    anchor("task", "Plan the seventeen-module change", goal="Bound the replay candidate", mode="analysis",
           risk="high", trust_domain="work", scope="Only the planned pkg modules.",
           acceptance_criteria=["The plan names exact paths"], output_format="dict")
    plan = {"schema_version": 1, "goal": "Set VALUE to 2 in every planned module", "risk": "high",
            "execution_mode": "source_change", "scope": ["Planned pkg modules"], "source_paths": PLANNED,
            "exclusions": [], "constraints": [], "risks": [], "stop_conditions": [],
            "unresolved_decisions": [],
            "destination": {"kind": "github_ref", "repository": "fixture/never-published",
                            "ref": "refs/heads/main"},
            "reconciliation": {"requirements": [{"id": "learning", "method": "producer_learning"}]},
            "criteria": [{"id": "replay", "expected": "Every planned module has VALUE 2", "method": "pytest",
                          "test_targets": ["test_replay.py"]}]}
    workflow_id = anchor("workflow", "create", plan=plan, request_id="replay-create",
                         output_format="dict")["state"]["workflow_id"]
    anchor("learning", "assess", outcome="nothing_reusable_learned", output_format="dict")

    # Producer: the Spec requirement is visible at acceptance, not first at touched.
    anchor("new_session", name="replay_producer", inline=True, output_format="dict")
    producer = anchor("task", "Apply the seventeen-module change", goal="Set VALUE to 2 everywhere planned",
                      mode="implementation", work_type="change", execution_mode="source_change",
                      risk="high", trust_domain="work", workflow_id=workflow_id,
                      acceptance_criteria=["Every planned module has VALUE 2"], output_format="dict")
    required = {item["id"] for item in producer["operating_protocol"]["required_now"]}
    assert {"load_required_skill", "required_spec"} <= required
    anchor("skill_loaded", "writing-specs", output_format="dict")
    created = anchor("spec", "create", name="REPLAY_CHANGE", output_format="dict")
    Path(created["path"]).write_text(SPEC)
    assert anchor("spec", "review", "REPLAY_CHANGE", output_format="dict")["rating"] in {"good", "excellent"}
    anchor("spec", "execute", "REPLAY_CHANGE", output_format="dict")
    assert anchor("status", output_format="dict")["operating_protocol"]["required_now"] == []
    _advance(anchor, "accept_plan")

    # More than the high-risk cap (10) of planned registrations, none blocked.
    anchor("known_bad", changed_files=PLANNED, output_format="dict")
    for path in PLANNED:
        (target / path).write_text("VALUE = 2\n")
        anchor("touched", path, output_format="dict")
    _git(target, "add", ".")
    _git(target, "commit", "-m", "replay candidate")

    # A self-hosted edit makes the dispatcher stale; the fresh process restores byte-bound state.
    anchor, _, _ = init(root=str(target), project="replay", output_format="dict")
    rebound = anchor("task_rebind", output_format="dict")
    assert rebound["task_window_id"] == producer["task_window_id"]
    assert rebound["continuity"]["restored"]["touched"] == sorted(PLANNED)
    assert rebound["continuity"]["not_restored"] == []
    assert [op["action"] for op in rebound["required_next_operations"]] == ["orient"]
    anchor("orient", output_format="dict")

    anchor("preflight", output_format="dict")
    assert anchor("test", target=["test_replay.py"], request_id="replay-tests",
                  output_format="dict")["metrics"]["passed"] == 1
    review = anchor("review", output_format="dict")
    assert not [item for item in review["findings"] if item.startswith("UNVERIFIED CRITERION")]
    assert _advance(anchor, "implemented")["progress"] == "implemented"
    measured = anchor("test", target=["test_replay.py"], workflow_criterion="replay", timeout=120,
                      request_id="replay-measure", output_format="dict")
    assert measured["workflow_measurement"]["status"] == "satisfied"
    anchor("gate", output_format="dict")
    anchor("learning", "assess", outcome="nothing_reusable_learned", output_format="dict")

    # High risk: a separate accepted read-only review task, then qualification.
    anchor("new_session", name="replay_review", inline=True, output_format="dict")
    anchor("task", "Review the seventeen-module candidate", goal="Verify the exact replay candidate",
           mode="review", execution_mode="read_only", risk="high", trust_domain="work",
           workflow_id=workflow_id, acceptance_criteria=["Review the exact implemented bytes"],
           output_format="dict")
    anchor("skill_loaded", "code-comprehension", output_format="dict")
    reviewed = _advance(anchor, "review", findings=[{"summary": "All planned modules changed", "status": "resolved"}])
    assert reviewed["review_result"]["separate_read_only_accepted_task"] is True
    qualified = _advance(anchor, "qualify")
    assert qualified["progress"] == "qualified" and qualified["completed"] is False
    assert sorted(qualified["candidate"]["snapshot"]["changed_paths"]) == sorted(PLANNED)
