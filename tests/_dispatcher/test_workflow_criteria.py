"""Command and review criteria qualify work that has no test file, in a named environment."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest


def _git(target, *args):
    return subprocess.run(["git", *args], cwd=target, check=True, capture_output=True, text=True).stdout


def advance(anchor, command, **kwargs):
    current = anchor("workflow", output_format="dict")["state"]
    return anchor("workflow", command, expected_generation=current["generation"],
                  request_id=f"{command}:{current['generation']}", output_format="dict", **kwargs)["state"]


@pytest.fixture
def docs(tmp_path, monkeypatch):
    """A git target holding only documentation, with a bound docs-only producer."""
    from odibi_anchor._dispatcher._project import project_action
    from odibi_anchor.bootstrap import init

    home, target = tmp_path / "home", tmp_path / "target"
    home.mkdir()
    (target / "docs").mkdir(parents=True)
    (target / "docs/guide.md").write_text("# Guide\n")
    _git(target, "init", "-b", "main")
    _git(target, "add", ".")
    _git(target, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-m", "base")
    monkeypatch.setenv("ANCHOR_HOME", str(home))
    monkeypatch.setenv("ANCHOR_MEMORY_DB", str(home / "memory.db"))
    monkeypatch.setenv("ANCHOR_TRUST_DOMAIN", "personal")
    project_action(home, "create", name="alpha", target=target, output_format="dict")
    anchor, _, _ = init(root=str(target), project="alpha", output_format="dict")
    anchor("orient", output_format="dict")
    anchor("new_session", name="plan", inline=True, output_format="dict")
    anchor("task", "Plan guide change", goal="Document the rollout", mode="analysis", risk="low",
           rigor="direct", trust_domain="personal", acceptance_criteria=["A bounded plan exists"],
           output_format="dict")
    plan = {"schema_version": 1, "goal": "Document the rollout", "risk": "low",
            "execution_mode": "source_change", "scope": ["docs/guide.md"], "source_paths": ["docs/guide.md"],
            "exclusions": [], "constraints": [], "risks": [], "stop_conditions": [],
            "unresolved_decisions": [],
            "destination": {"kind": "github_ref", "repository": "acme/app", "ref": "refs/heads/main"},
            "reconciliation": {"requirements": [], "reason": "Disposable fixture"},
            "criteria": [
                {"id": "docs", "expected": "The guide documents the rollout", "method": "command",
                 "environment": "windows", "argv": [
                     "python", "-c", "import os, pathlib, sys; "
                     "sys.exit(0 if os.environ['DOC_WORD'] in pathlib.Path('docs/guide.md').read_text() else 1)"]},
                {"id": "clear", "expected": "The guide reads clearly", "method": "review"},
            ]}
    assert anchor("workflow", "check_plan", plan=plan, output_format="dict")["ready"] is True
    draft = anchor("workflow", "create", plan=plan, request_id="draft", output_format="dict")["state"]
    anchor("learning", "assess", outcome="nothing_reusable_learned", output_format="dict")
    anchor("new_session", name="produce", inline=True, output_format="dict")
    anchor("task", "Document the rollout", goal="Document the rollout", mode="implementation",
           work_type="change", execution_mode="source_change", risk="low", trust_domain="personal",
           workflow_id=draft["workflow_id"], acceptance_criteria=["The guide documents the rollout"],
           output_format="dict")
    advance(anchor, "accept_plan")
    anchor("known_bad", changed_files=["docs/guide.md"], output_format="dict")
    (target / "docs/guide.md").write_text("# Guide\n\nRollout: enable the flag per site.\n")
    anchor("touched", "docs/guide.md", output_format="dict")
    _git(target, "add", ".")
    _git(target, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-m", "guide")
    advance(anchor, "implemented")
    return anchor


@pytest.mark.timeout(600)  # two sessions and a full producer lifecycle; slow on loaded Windows hosts
def test_docs_only_command_and_review_criteria_qualify_without_a_test_file(docs, monkeypatch):
    anchor = docs
    monkeypatch.setenv("ANCHOR_TEST_ENVIRONMENTS", json.dumps({"wsl-spark": {
        "kind": "wsl", "distro": "Ubuntu", "python": "/usr/bin/python3"}}))
    with pytest.raises(ValueError, match=r"'windows' is not configured.*wsl-spark.*test_environments") as unknown:
        anchor("test", workflow_criterion="docs", output_format="dict")
    assert unknown.value.context["executed"] is False
    assert anchor("workflow", output_format="dict")["state"]["measurements"] == {}

    monkeypatch.setenv("ANCHOR_TEST_ENVIRONMENTS", json.dumps({"windows": {
        "kind": "local", "python": sys.executable, "env": {"DOC_WORD": "Rollout"}}}))
    result = anchor("test", workflow_criterion="docs", output_format="dict")
    assert result["kind"] == "command_run" and result["workflow_measurement"]["status"] == "satisfied"
    measured = anchor("workflow", output_format="dict")["state"]["measurements"]["docs"]
    assert measured["collector"] == "anchor.command_runner" and measured["result"]["exit_code"] == 0
    assert (measured["environment"]["name"], measured["environment"]["kind"]) == ("windows", "local")
    assert measured["environment"]["executable"] == sys.executable

    advance(anchor, "review", findings=[{"summary": "Example missing", "status": "open"}])
    anchor("preflight", output_format="dict")
    anchor("review", output_format="dict")
    anchor("gate", output_format="dict")
    anchor("learning", "assess", outcome="nothing_reusable_learned",
           notes="Negative paths in this fixture are deliberate.", output_format="dict")
    with pytest.raises(RuntimeError, match="not satisfied"):
        advance(anchor, "qualify")
    advance(anchor, "review", findings=[{"summary": "Example missing", "status": "resolved"}])
    qualified = advance(anchor, "qualify")
    assert qualified["progress"] == "qualified"
    review_check = next(c for c in qualified["qualification"]["checks"] if c["criterion_id"] == "clear")
    assert review_check["collector"] == "anchor.workflow_review"
    assert review_check["evidence_ref"] == qualified["review_result"]["evidence_ref"]
