"""Issue26: discover paths without changing target-relative registration authority."""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


def test_saved_artifacts_are_exact_project_scoped_paginated_and_nonmutating(tmp_path, capsys, monkeypatch):
    from odibi_anchor import cli
    from odibi_anchor._dispatcher._problem import problem_action
    from odibi_anchor._dispatcher._project import project_action
    from odibi_anchor._dispatcher._spec import spec_action
    from odibi_anchor._dispatcher._work_item import work_item_action
    from odibi_anchor.artifacts import inspect_artifacts

    for project in ("alpha", "beta"):
        project_action(tmp_path, "create", name=project, output_format="dict")
        root = tmp_path / "workspace/projects" / project
        spec_action(root, "create", "A", content=f"# {project}\r\n## Saved\r\nλ exact\r\n")
        spec_action(root, "create", "A_LONG", content="# Not the same spec\n")
        work_item_action(root, "create", title=project, outcome="A saved draft")
        problem_action(root, "create", title=project, project_id=project)
        (root / "decisions/choice.md").write_text("# Choice\nKeep the asymmetry.\n")
        (root / "notebooks/handoff.json").write_text('{"saved":true}')
    roots = tmp_path / "workspace/projects"
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in tmp_path.rglob("*") if p.is_file()}
    inventories = [inspect_artifacts(anchor_home=tmp_path, project_id=p) for p in ("alpha", "beta")]
    assert inventories[0]["total"] == 7
    alpha = inventories[0]
    assert all(x["project_id"] == "alpha" for x in alpha["artifacts"])
    assert {x["artifact_id"] for x in alpha["artifacts"]}.isdisjoint(
        {x["artifact_id"] for x in inventories[1]["artifacts"]})
    page = inspect_artifacts(anchor_home=tmp_path, project_id="alpha", limit=2)
    assert page["next_offset"] == 2 and len(page["artifacts"]) == 2
    a_root = roots / "alpha"
    for item in alpha["artifacts"]:
        relative = item["relative_path"]
        data = (a_root / relative).read_bytes()
        saved = inspect_artifacts(anchor_home=tmp_path, project_id="alpha", path=relative, limit=5)
        assert saved["content_sha256"] == hashlib.sha256(data).hexdigest() == item["content_sha256"]
        text = saved["content"]
        while saved["next_offset"] is not None:
            saved = inspect_artifacts(anchor_home=tmp_path, project_id="alpha", path=relative,
                                      offset=saved["next_offset"], limit=5, expected_sha256=saved["content_sha256"])
            text += saved["content"]
        assert text == data.decode("utf-8")
        assert inspect_artifacts(anchor_home=tmp_path, project_id="alpha", path=relative,
                                 output_format="markdown") == text
    shown = spec_action(a_root, "show", "A_SPEC.md", output_format="dict")
    assert shown["content"] == "# alpha\r\n## Saved\r\nλ exact\r\n"
    with pytest.raises(FileNotFoundError):
        spec_action(a_root, "show", "A_LO", output_format="dict")
    wi_list = work_item_action(a_root, "list", output_format="dict")
    prb_list = problem_action(a_root, "list", output_format="dict")
    assert isinstance(wi_list, dict) and isinstance(prb_list, dict)
    wi, prb = wi_list["work_items"][0], prb_list["problems"][0]
    for action, record, key in ((work_item_action, wi, "work_item_id"), (problem_action, prb, "problem_id")):
        shown = action(a_root, "show", record[key], output_format="dict")
        assert isinstance(shown, dict)
        assert shown["record"] and shown["content"] == (a_root / shown["relative_path"]).read_text()
        human = action(a_root, "show", record[key])
        assert isinstance(human, str) and human.endswith(shown["content"])
    monkeypatch.setattr(cli, "_boot", lambda *_: pytest.fail("read bootstrapped a runtime"))
    assert cli.main(["artifacts", "--home", str(tmp_path), "--project", "alpha", "--path", "specs/A_SPEC.md"]) == 0
    assert json.loads(capsys.readouterr().out)["result"]["content_sha256"] == hashlib.sha256((a_root / "specs/A_SPEC.md").read_bytes()).hexdigest()
    assert {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in tmp_path.rglob("*") if p.is_file()} == before


def test_saved_artifact_reads_refuse_unsafe_missing_unavailable_and_stale_pages(tmp_path):
    from odibi_anchor._dispatcher._project import project_action
    from odibi_anchor.artifacts import inspect_artifacts

    project_action(tmp_path, "create", name="alpha", output_format="dict")
    root = tmp_path / "workspace/projects/alpha"
    path = root / "specs/A_SPEC.md"
    path.write_text("# One\n## Two\n")
    first = inspect_artifacts(anchor_home=tmp_path, project_id="alpha", path="specs/A_SPEC.md", limit=3)
    with pytest.raises(ValueError, match="require expected_sha256"):
        inspect_artifacts(anchor_home=tmp_path, project_id="alpha", path="specs/A_SPEC.md", offset=3)
    path.write_text("Changed\n")
    with pytest.raises(ValueError, match="stale"):
        inspect_artifacts(anchor_home=tmp_path, project_id="alpha", path="specs/A_SPEC.md", offset=3,
                          expected_sha256=first["content_sha256"])
    with pytest.raises(FileNotFoundError):
        inspect_artifacts(anchor_home=tmp_path, project_id="alpha", path="specs/MISSING_SPEC.md")
    for relative in ("../beta/specs/A_SPEC.md", "/etc/passwd", ".odibi-anchor/state.db"):
        with pytest.raises(ValueError):
            inspect_artifacts(anchor_home=tmp_path, project_id="alpha", path=relative)
    path.unlink()
    path.symlink_to(tmp_path / "foreign.md")
    with pytest.raises(ValueError, match="symlink"):
        inspect_artifacts(anchor_home=tmp_path, project_id="alpha", path="specs/A_SPEC.md")
    assert inspect_artifacts(anchor_home=tmp_path, project_id="alpha")["unavailable"]


def test_project_contract_discovers_absolute_paths_without_writes(tmp_path):
    from odibi_anchor._dispatcher._project import project_action

    target = tmp_path / "target"
    target.mkdir()
    project_action(tmp_path, "create", name="alpha", target=target, output_format="dict")
    artifact = tmp_path / "workspace/projects/alpha"
    before = (artifact / "PROJECT.md").read_bytes()
    status = project_action(tmp_path, "status", output_format="dict")
    paths = {item["path"]: item for item in status["artifact_contract"]["artifacts"]}
    assert paths["PROJECT.md"]["absolute_path"] == str(artifact / "PROJECT.md")
    assert paths["notebooks/"]["absolute_path"] == str(artifact / "notebooks")
    assert status["artifact_contract"]["path_semantics"]["relative_touched_base"] == "target_root"
    assert status["artifact_contract"]["path_semantics"]["authority_granted"] is False
    assert (artifact / "PROJECT.md").read_bytes() == before
    rendered = project_action(tmp_path, "status", output_format="markdown")
    assert str(artifact / "PROJECT.md") in rendered
    assert "target_root" in rendered


@pytest.mark.parametrize("replacement", ["outside_link", "inside_link", "root_link"])
def test_discovery_withholds_symlinked_paths(tmp_path, replacement):
    from odibi_anchor._dispatcher._project import artifact_contract

    root = tmp_path / "artifacts"
    root.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    if replacement == "root_link":
        root.rmdir()
        root.symlink_to(other, target_is_directory=True)
    else:
        (root / "notebooks").symlink_to(other if replacement == "outside_link" else root,
                                      target_is_directory=True)
    contract = artifact_contract(artifact_root=str(root))
    entry = next(item for item in contract["artifacts"] if item["path"] == "notebooks/")
    assert entry["absolute_path"] is None
    assert entry["path_status"] == "unavailable"
    assert "symlink" in entry["path_reason"]


def test_bound_discovery_does_not_follow_legacy_project_selection(tmp_path):
    from odibi_anchor._dispatcher._project import project_action, resolve_route_binding

    project_action(tmp_path, "create", name="alpha", output_format="dict")
    binding = resolve_route_binding(tmp_path, project="alpha", runtime_instance_id="test")
    project_action(tmp_path, "create", name="beta", output_format="dict")
    result = project_action(tmp_path, "status", route_binding=binding, output_format="dict")
    entry = result["artifact_contract"]["artifacts"][0]
    assert entry["absolute_path"] == str(tmp_path / "workspace/projects/alpha/PROJECT.md")


PROBE = r'''
import hashlib
import sys
from pathlib import Path
from odibi_anchor.startup import launch, register_project

base, variant = Path(sys.argv[1]), sys.argv[2]
home = base / "home"
target = base / "Workspace/Users/synthetic/_scratch"
target.mkdir(parents=True)
(target / "source.txt").write_text("Original target bytes\n")
if variant == "ambiguous":
    (target / "PROJECT.md").write_text("Target project, not managed\n")
register_project(anchor_home=home, project_id="demo", project_root=target)
anchor = launch(anchor_home=home, project_id="demo", project_root=target)
orientation = anchor("orient", output_format="dict")
entry = next(item for item in orientation["artifact_contract"]["artifacts"] if item["path"] == "PROJECT.md")
artifact = home / "workspace/projects/demo/PROJECT.md"
assert entry["absolute_path"] == str(artifact)
assert entry["path_status"] == "available"
assert "absolute_path" in str(anchor("help", "touched", output_format="dict"))
assert "target_root" in str(anchor("help", "touched", output_format="dict"))
assert "artifact_contract" in str(anchor("help", "project", output_format="dict"))
session = anchor("new_session", name="plan", inline=True, output_format="dict")
task = anchor("task", "Plan managed project audit", goal="Prove exact managed bytes", mode="analysis",
              risk="low", rigor="direct", trust_domain="personal",
              acceptance_criteria=["Exact managed bytes"], output_format="dict")
assert task["artifact_contract"]["artifacts"][0]["absolute_path"] == str(artifact)
if variant == "read_only":
    try:
        result = anchor("touched", str(artifact), output_format="dict")
    except RuntimeError as exc:
        assert "incompatible" in str(exc), str(exc)
    else:
        raise AssertionError("Discovery granted write permission")
    raise SystemExit(0)
audited = artifact.read_bytes() + b"\nSynthetic audit\n"
plan = {"schema_version": 1, "goal": "Qualify managed project audit", "risk": "medium",
        "execution_mode": "artifact_only", "scope": ["PROJECT.md"], "artifact_paths": ["PROJECT.md"],
        "exclusions": [], "constraints": [], "risks": [], "stop_conditions": [],
        "unresolved_decisions": [], "destination": {"kind": "managed_artifacts"},
        "reconciliation": {"requirements": [], "reason": "Synthetic artifact only"},
        "criteria": [{"id": "audit", "expected": "Exact audit bytes", "method": "artifact_sha256",
                      "expected_sha256": {"PROJECT.md": hashlib.sha256(audited).hexdigest()}}]}
draft = anchor("workflow", "create", plan=plan, request_id="draft", output_format="dict")["state"]
closed = anchor("learning", "assess", outcome="nothing_reusable_learned", output_format="dict")
session = anchor("new_session", name="producer", inline=True, output_format="dict")
task = anchor("task", "Implement managed project audit", goal="Prove exact managed bytes",
              mode="implementation", execution_mode="artifact_only", risk="medium", rigor="compact",
              trust_domain="personal", workflow_id=draft["workflow_id"],
              acceptance_criteria=["Exact managed bytes"], output_format="dict")
for name in task["required_skills"]:
    skill = anchor("skill_loaded", name, output_format="dict")
def advance(command, **kwargs):
    current = anchor("workflow", output_format="dict")["state"]
    return anchor("workflow", command, expected_generation=current["generation"],
                  request_id=f"{command}:{current['generation']}", output_format="dict", **kwargs)
if variant != "draft_drift":
    accepted = advance("accept_plan")
artifact.write_bytes(audited)
anchor = launch(anchor_home=home, project_id="demo", project_root=target)
orientation = anchor("orient", output_format="dict")
rebound = anchor("task_rebind", task_window_id=task["task_window_id"], output_format="dict")
entry = orientation["artifact_contract"]["artifacts"][0]
assert entry["absolute_path"] == str(artifact)
if variant == "draft_drift":
    try:
        accepted = advance("accept_plan")
    except RuntimeError as exc:
        assert "pre-plan artifact changed" in str(exc), str(exc)
    else:
        raise AssertionError("Discovery laundered draft bytes")
    raise SystemExit(0)
supplied = "PROJECT.md" if variant in {"relative", "ambiguous"} else entry["absolute_path"]
registration = anchor("touched", supplied, output_format="dict")
if variant in {"relative", "ambiguous"}:
    assert registration["path_resolution"]["absolute_path"] == str(target / "PROJECT.md")
    assert registration["path_resolution"]["basis"] == "target_root"
    assert "managed_artifact" not in registration
else:
    assert "managed_artifact" in registration
    assert "cross_project_warning" not in registration
if variant == "target_drift":
    (target / "source.txt").write_text("Unauthorized target change\n")
implemented = advance("implemented")
measured = advance("check_artifact", criterion_id="audit")
reviewed = advance("review", findings=[])
review = anchor("review", output_format="dict")
if variant in {"relative", "ambiguous", "target_drift"}:
    try:
        gate = anchor("gate", output_format="dict")
    except RuntimeError as exc:
        message = str(exc)
        assert "Non-artifact files" in message
        assert "registered or detected" in message
        assert "target_root" in message and str(target) in message
        assert "project" in message and "absolute_path" in message
        assert ("PROJECT.md" if variant != "target_drift" else "source.txt") in message
    else:
        raise AssertionError("Target-relative registration or drift was reclassified")
else:
    gate = anchor("gate", output_format="dict")
    closed = anchor("learning", "assess", outcome="nothing_reusable_learned", output_format="dict")
    qualified = advance("qualify")
    assert qualified["state"]["progress"] == "qualified"
'''


@pytest.mark.parametrize("variant", ["absolute", "relative", "ambiguous", "target_drift", "read_only", "draft_drift"])
def test_public_artifact_discovery_registration_and_rebind(tmp_path, variant):
    import odibi_anchor
    from odibi_anchor.pytest_runner import child_environment

    environment = {**child_environment(), "ANCHOR_TRUST_DOMAIN": "personal",
                   "PYTHONPATH": str(Path(odibi_anchor.__file__).resolve().parent.parent)}
    result = subprocess.run([sys.executable, "-c", PROBE, str(tmp_path), variant],
                            env=environment, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("layout", ["equal", "artifact_nested", "target_nested", "sibling_prefix"])
def test_path_classification_stays_root_based(tmp_path, layout):
    from odibi_anchor._dispatcher._enforcement import should_block_mode_mismatch
    from odibi_anchor._dispatcher._project import artifact_contract

    artifact = tmp_path / "managed"
    target = {"equal": artifact, "artifact_nested": tmp_path,
              "target_nested": artifact / "source/app", "sibling_prefix": tmp_path / "managed-target"}[layout]
    artifact.mkdir()
    target.mkdir(parents=True, exist_ok=True)
    discovered = artifact_contract(artifact_root=str(artifact / "."))["artifacts"][0]["absolute_path"]
    assert discovered == str(artifact / "PROJECT.md")
    def blocked(path):
        return should_block_mode_mismatch([], {str(path)}, artifact_root=str(artifact),
                                          target_root=str(target), execution_mode="artifact_only")[0]
    assert not blocked(discovered)
    # Equal roots and managed source/reference paths keep their existing taxonomy.
    assert blocked("PROJECT.md") is (layout in {"artifact_nested", "sibling_prefix"})
    assert blocked(artifact / ".." / "escape.md")
    assert not os.path.islink(discovered)


def test_path_replacement_invalidates_discovery_without_granting_authority(tmp_path):
    from odibi_anchor._dispatcher._enforcement import should_block_mode_mismatch
    from odibi_anchor._dispatcher._project import artifact_contract

    root = tmp_path / "artifacts"
    folder = root / "notebooks"
    folder.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    before = artifact_contract(artifact_root=str(root))["artifacts"]
    assert next(item for item in before if item["path"] == "notebooks/")["absolute_path"] == str(folder)
    folder.rmdir()
    folder.symlink_to(outside, target_is_directory=True)
    after = artifact_contract(artifact_root=str(root))["artifacts"]
    assert next(item for item in after if item["path"] == "notebooks/")["absolute_path"] is None
    blocked, _ = should_block_mode_mismatch(
        [], {str(folder / "escaped.md")}, artifact_root=str(root),
        target_root=str(outside), execution_mode="artifact_only",
    )
    assert blocked


def test_gate_diagnostic_does_not_claim_registration_proves_byte_changes(tmp_path):
    from odibi_anchor._dispatcher._enforcement import should_block_mode_mismatch

    blocked, message = should_block_mode_mismatch(
        [], {"PROJECT.md"}, artifact_root=str(tmp_path / "managed"),
        target_root=str(tmp_path / "target"), execution_mode="artifact_only",
    )
    assert blocked
    assert "registered or detected" in message
    assert "Registration alone does not prove a byte change" in message
    assert "target-root files were modified" not in message
    assert "absolute_path" in message


def test_nonmanaged_sibling_prefix_still_gets_cross_project_warning(tmp_path):
    from odibi_anchor._dispatcher._session import _touched_action

    root = tmp_path / "target"
    root.mkdir()
    sibling = tmp_path / "target-other/file.md"
    result = _touched_action(str(sibling), str(root))
    assert "cross_project_warning" in result
    assert result["path_resolution"]["absolute_path"] == str(sibling)
    assert result["path_resolution"]["classification"] == "source_ledger"
