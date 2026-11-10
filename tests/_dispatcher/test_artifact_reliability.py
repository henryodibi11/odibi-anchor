"""Saved artifact contracts through native actions and non-bootstrapping public APIs."""

import hashlib
import json
from pathlib import Path

import pytest

from odibi_anchor._dispatcher._problem import problem_action
from odibi_anchor._dispatcher._project import project_action
from odibi_anchor._dispatcher._spec import spec_action
from odibi_anchor.artifacts import inspect_readiness


def wi(root, *args, **kwargs):
    # Bootstrap compatibility tests can replace the imported package modules.
    from odibi_anchor._dispatcher._work_item import work_item_action

    result = work_item_action(root, *args, output_format="dict", **kwargs)
    assert isinstance(result, dict)
    return result


def problem(root, *args, **kwargs):
    result = problem_action(root, *args, output_format="dict", **kwargs)
    assert isinstance(result, dict)
    return result


def spec(root, *args, **kwargs):
    return spec_action(root, *args, output_format="dict", **kwargs)


def bytes_and_times(root):
    return {p.relative_to(root).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in root.rglob("*") if p.is_file()}


PLAN = {
    "title": "Preserve saved export decisions", "outcome": "Reviewers recover the exact saved plan",
    "context": "Receipt history can be empty even when files exist", "scope": "Native saved records",
    "non_goals": "No host permission changes", "acceptance_criteria": ["Exact readback from a fresh process"],
    "implementation_notes": "Add native reads using the existing registry",
    "validation_notes": "Compare exact bytes with asymmetric project controls",
    "risks": "File replacement can invalidate a read", "dependencies": "N/A: This is a standalone local feature",
}


def test_native_stale_updates_and_invalid_fields_never_write(tmp_path):
    item = wi(tmp_path, "create", **PLAN)
    prb = problem(tmp_path, "create", title="Choose a key", definition="Distinct IDs", write_version=2)
    for call, created, id_key in ((wi, item, "work_item_id"), (problem, prb, "problem_id")):
        before = bytes_and_times(tmp_path)
        for args in ({"unexpected": "silently lost"}, {"scope": {"wrong": "type"}}):
            with pytest.raises(ValueError):
                call(tmp_path, "update", created[id_key], write_version=2,
                     expected_revision=created["revision"], **args)
            assert bytes_and_times(tmp_path) == before
        changed = call(tmp_path, "update", created[id_key], scope="A different exact scope", write_version=2,
                       expected_sha256=created["content_sha256"])
        saved = bytes_and_times(tmp_path)
        for expected in ({"expected_revision": created["revision"]}, {"expected_sha256": created["content_sha256"]}):
            with pytest.raises(ValueError, match="stale"):
                call(tmp_path, "update", created[id_key], scope="Stale overwrite", write_version=2, **expected)
            assert bytes_and_times(tmp_path) == saved
        shown = call(tmp_path, "show", created[id_key])
        assert shown["record"]["scope"] == "A different exact scope"
        assert changed["revision"] == created["revision"] + 1
    before = bytes_and_times(tmp_path)
    for row in ({"source": "synthetic", "unknown_cell": "would disappear"},
                {"source": "synthetic", "Source": "conflicting alias"},
                {"source": "synthetic", "observation": {"nested": "not a cell"}}):
        with pytest.raises(ValueError):
            problem(tmp_path, "update", prb["problem_id"], evidence=row, write_version=2,
                    expected_revision=2)
        assert bytes_and_times(tmp_path) == before
    accepted = problem(tmp_path, "update", prb["problem_id"], write_version=2, expected_revision=2,
                       evidence={"source": "synthetic query | safe", "observation": "a\nb & c", "contradicts": True})
    row = problem(tmp_path, "show", prb["problem_id"])["record"]["evidence"][0]
    assert row["Source"] == "synthetic query | safe" and row["Observation"] == "a\nb & c" and row["Contradicts"] == "yes"
    assert accepted["write_performed"]
    legacy = problem(tmp_path, "update", prb["problem_id"], scope=42, write_version=1)
    assert legacy["warnings"] and problem(tmp_path, "show", prb["problem_id"])["record"]["scope"] == "42"


def test_spec_safe_full_replacement_preview_and_intentional_removal(tmp_path):
    original = "# Design\n\n## Constraints\nKeep private data private.\n\n## Validation\nCompare exact bytes.\n"
    created = spec(tmp_path, "create", "DESIGN", content=original)
    desired = "# Design\n\n## Validation\nThe scope deliberately changed.\n"
    safe = {"write_version": 2, "expected_sha256": created["sha256"], "replacement": True}
    before = bytes_and_times(tmp_path)
    preview = spec(tmp_path, "preview_update", "DESIGN", content=desired, **safe)
    assert preview["removed_sections"] == ["## Constraints"] and preview["write_performed"] is False
    assert bytes_and_times(tmp_path) == before
    with pytest.raises(ValueError, match="removes sections"):
        spec(tmp_path, "update", "DESIGN", content=desired, **safe)
    for invalid in ({"unexpected": "field"}, {"replacement": "true"}, {"allow_destructive": "yes"}):
        with pytest.raises(ValueError):
            spec(tmp_path, "update", "DESIGN", content=desired, **{**safe, **invalid})
        assert bytes_and_times(tmp_path) == before
    saved = spec(tmp_path, "update", "DESIGN", content=desired, allow_destructive=True, **safe)
    assert saved["write_performed"] and spec(tmp_path, "show", "DESIGN")["content"] == desired
    before = bytes_and_times(tmp_path)
    with pytest.raises(ValueError, match="stale spec"):
        spec(tmp_path, "update", "DESIGN", content=original, **safe)
    assert bytes_and_times(tmp_path) == before
    assert saved["content_sha256"] == hashlib.sha256(desired.encode()).hexdigest()


def test_pure_readiness_and_plan_approval_share_rules_without_requiring_implementation(tmp_path, monkeypatch):
    import odibi_anchor.owner_approval as owner

    decisions = []
    monkeypatch.setattr(owner, "request_owner_approval", lambda **kw: decisions.append(kw) or {"status": "approved"})
    project_action(tmp_path, "create", name="alpha", output_format="dict")
    root = tmp_path / "workspace/projects/alpha"
    item = wi(root, "create", **{**PLAN, "implementation_notes": "Pending: Need a key decision; next: ask owner"})
    item_id = item["work_item_id"]
    subject = wi(root, "review_subject", item_id)["subject"]
    reviewed = wi(root, "review", item_id, subject_type="plan", expected_subject=subject,
                  expected_fingerprint=item["fingerprint"], angle="design", verdict="approve", findings=[],
                  reviewer_ref="caller-claim-not-authentication")
    before = bytes_and_times(tmp_path)
    readiness = inspect_readiness(anchor_home=tmp_path, project_id="alpha", kind="work_item", identifier=item_id)
    assert readiness["content_addressed_for_review"] and not readiness["approval_ready"]
    assert readiness["content"][item_id]["fields"]["implementation_notes"]["state"] == "pending"
    with pytest.raises(ValueError, match="pending"):
        wi(root, "request_plan_approval", work_items=[item_id], rigor="direct", readiness_version=2)
    assert not decisions and bytes_and_times(tmp_path) == before
    changed = wi(root, "update", item_id, implementation_notes=PLAN["implementation_notes"], write_version=2,
                 expected_revision=reviewed["revision"])
    subject = wi(root, "review_subject", item_id)["subject"]
    assert subject["source_revision"] is None and subject["subject_type"] == "plan"
    reviewed = wi(root, "review", item_id, expected_subject=subject, expected_fingerprint=changed["fingerprint"],
                  angle="design", verdict="approve", findings=[], reviewer_ref="another-caller-claim")
    before = bytes_and_times(tmp_path)
    readiness = wi(root, "check_plan", item_id, rigor="direct")
    assert readiness["approval_ready"] and not readiness["execution_admissible"] and not readiness["semantic_certification"]
    assert bytes_and_times(tmp_path) == before
    approved = wi(root, "request_plan_approval", work_items=[item_id], rigor="direct", readiness_version=2)
    assert approved["readiness"] == readiness and approved["write_performed"]
    assert approved["plan_approvals"][0]["fingerprint"] == changed["fingerprint"]
    assert approved["subject"]["readiness_version"] == 2
    assert wi(root, "show", item_id)["fingerprint"] == changed["fingerprint"]
    assert wi(root, "show", item_id)["content_sha256"] != reviewed["content_sha256"]


def test_review_rejects_foreign_stale_spec_dependency_and_implementation_subjects(tmp_path):
    for name in ("alpha", "beta"):
        root = tmp_path / name
        root.mkdir()
        spec(root, "create", "DESIGN", content="# Design\n## Success Criteria\n- Exact reads\n")
        wi(root, "create", **PLAN, specification="DESIGN")
    root = tmp_path / "alpha"
    item = wi(root, "list")["work_items"][0]
    item_id = item["work_item_id"]
    current = wi(root, "review_subject", item_id)["subject"]
    foreign = wi(tmp_path / "beta", "review_subject", item_id)["subject"]
    values = {"angle": "design", "verdict": "approve", "findings": [], "reviewer_ref": "claimed",
              "expected_fingerprint": item["fingerprint"]}
    before = bytes_and_times(tmp_path)
    with pytest.raises(ValueError, match="foreign"):
        wi(root, "review", item_id, expected_subject=foreign, **values)
    with pytest.raises(ValueError, match="implementation review uses workflow"):
        wi(root, "review", item_id, subject_type="implementation", **values)
    assert bytes_and_times(tmp_path) == before
    recorded = wi(root, "review", item_id, expected_subject=current, **values)
    # Operational ledger writes change full content, not the material subject.
    again = wi(root, "review_subject", item_id)["subject"]
    assert again["binding_sha256"] == current["binding_sha256"] and again["content_sha256"] != current["content_sha256"]
    spec(root, "update", "DESIGN", content="# Design\n## Success Criteria\n- Different scope\n")
    before = bytes_and_times(tmp_path)
    with pytest.raises(ValueError, match="stale"):
        wi(root, "review", item_id, expected_subject=current, **values)
    assert bytes_and_times(tmp_path) == before
    assert wi(root, "show", item_id)["reviews"][0]["stale"]
    assert not wi(root, "check_plan", item_id, spec="DESIGN")["approval_ready"]
    assert recorded["review"]["reviewer_authentication"] == "none"
    dependency = wi(root, "create", title="Upstream", outcome="Saved")
    fresh = wi(root, "review_subject", item_id)["subject"]
    wi(root, "update", item_id, depends_on=[dependency["work_item_id"]])
    assert wi(root, "show", item_id)["fingerprint"] == item["fingerprint"]
    with pytest.raises(ValueError, match="stale"):
        wi(root, "review", item_id, expected_subject=fresh, **values)


def test_problem_transition_readiness_is_pure_and_matches_close(tmp_path):
    saved = problem(tmp_path, "create", title="Unknown grain", hypothesis={"hypothesis": "Two rows per key", "critical": True})
    before = bytes_and_times(tmp_path)
    check = problem(tmp_path, "check_readiness", saved["problem_id"], stage="close")["readiness"]
    assert not check["ready"]
    for command, values in (("close", {"next_action": {"invalid": "type"}}),
                            ("link_spec", {"spec": ["DESIGN"]}),
                            ("link_spec", {"spec": "DESIGN", "recommendation_revision": True})):
        with pytest.raises(ValueError):
            problem(tmp_path, command, saved["problem_id"], write_version=2,
                    expected_revision=saved["revision"], **values)
        assert bytes_and_times(tmp_path) == before
    with pytest.raises(ValueError) as caught:
        problem(tmp_path, "close", saved["problem_id"], outcome="inconclusive")
    assert str(caught.value) == "; ".join(check["problems"])
    assert bytes_and_times(tmp_path) == before
    problem(tmp_path, "update", saved["problem_id"], hypothesis={"id": "H1", "status": "inconclusive"})
    assert problem(tmp_path, "check_readiness", saved["problem_id"], stage="close")["readiness"]["ready"]
    assert problem(tmp_path, "close", saved["problem_id"], outcome="inconclusive")["closed"]


@pytest.mark.parametrize(("criterion", "state"), [
    ("TBD", "missing"), ("Pending: ; next: ask owner", "missing"),
    ("Pending: Need key choice; next: ask owner", "pending"), ("N/A: No remote sink", "not_applicable"),
])
def test_plan_criteria_dispositions_do_not_hide_in_lists(tmp_path, criterion, state):
    item = wi(tmp_path, "create", **{**PLAN, "acceptance_criteria": ["Recover exact bytes", criterion]})
    before = bytes_and_times(tmp_path)
    readiness = wi(tmp_path, "check_plan", item["work_item_id"])
    assert readiness["content"][item["work_item_id"]]["fields"]["acceptance_criteria"]["state"] == state
    assert readiness["content_addressed_for_review"] == (state != "missing")
    assert bytes_and_times(tmp_path) == before


def test_no_loss_updates_preserve_omitted_cells_and_require_destructive_opt_in(tmp_path):
    created = problem(tmp_path, "create", title="Keep decisions", write_version=2,
                      hypothesis={"hypothesis": "Two rows", "critical": True, "status": "deferred", "expected_signal": "Pairs"})
    patched = problem(tmp_path, "update", created["problem_id"], write_version=2, expected_revision=1,
                      hypothesis={"id": "H1", "expected_signal": ""})
    row = problem(tmp_path, "show", created["problem_id"])["record"]["hypotheses"][0]
    assert row["Critical"] == "yes" and row["Status"] == "deferred" and row["Hypothesis"] == "Two rows"
    assert row["Expected signal"] == ""
    path = Path(patched["artifact_path"])
    path.write_text(path.read_text() + "\n## Custom decision\nNever silently discard this.\n")
    shown = problem(tmp_path, "show", created["problem_id"])
    before = bytes_and_times(tmp_path)
    with pytest.raises(ValueError, match="losslessly"):
        problem(tmp_path, "update", created["problem_id"], write_version=2, expected_sha256=shown["content_sha256"],
                scope="Changed")
    assert bytes_and_times(tmp_path) == before
    problem(tmp_path, "update", created["problem_id"], write_version=2, expected_sha256=shown["content_sha256"],
            allow_destructive=True, scope="Changed")
    assert "Custom decision" not in path.read_text()
    before = bytes_and_times(tmp_path)
    for call, values in ((problem, {"title": "X", "definition": "[Not yet captured] but meaningful"}),
                         (wi, {"title": "X", "outcome": "[Not captured]"})):
        with pytest.raises(ValueError, match="round-trip"):
            call(tmp_path, "create", **values)
        assert bytes_and_times(tmp_path) == before


def test_default_readiness_holds_sparse_drafts_but_explicit_legacy_approval_remains(tmp_path, monkeypatch):
    import odibi_anchor.owner_approval as owner

    requested = []
    monkeypatch.setattr(owner, "request_owner_approval", lambda **kw: requested.append(kw) or {"status": "approved"})
    item = wi(tmp_path, "create", title="Draft", outcome="Explore options")
    item_id = item["work_item_id"]
    wi(tmp_path, "review", item_id, angle="design", verdict="approve", findings=[], reviewer_ref="untrusted",
       expected_fingerprint=item["fingerprint"])
    before = bytes_and_times(tmp_path)
    with pytest.raises(ValueError, match="context is missing"):
        wi(tmp_path, "request_plan_approval", work_items=[item_id], rigor="direct")
    assert not requested and bytes_and_times(tmp_path) == before
    approved = wi(tmp_path, "request_plan_approval", work_items=[item_id], rigor="direct", readiness_version=1)
    assert approved["readiness"]["limitations"][-1].startswith("Legacy")
    shown = wi(tmp_path, "show", item_id)
    assert shown["plan_approval"]["approved"]
    assert not shown["approval_projection"]["content"]["content_ready_for_approval"]


def test_owner_response_cannot_approve_a_changed_spec_and_projection_tracks_spec_drift(tmp_path, monkeypatch):
    import odibi_anchor.owner_approval as owner

    original = "# Design\n## Success Criteria\n- Recover exact saved bytes\n"
    spec(tmp_path, "create", "DESIGN", content=original)
    item = wi(tmp_path, "create", **PLAN, specification="DESIGN")
    item_id = item["work_item_id"]
    wi(tmp_path, "review", item_id, angle="design", verdict="approve", findings=[], reviewer_ref="claim",
       expected_fingerprint=item["fingerprint"])

    def drift(**kwargs):
        spec(tmp_path, "update", "DESIGN", content=original + "Changed by another writer\n")
        return {"status": "approved"}

    monkeypatch.setattr(owner, "request_owner_approval", drift)
    with pytest.raises(ValueError, match="spec changed while approval was pending"):
        wi(tmp_path, "request_plan_approval", work_items=[item_id], rigor="direct", spec="DESIGN")
    assert wi(tmp_path, "show", item_id)["plan_approvals"] == []
    # Restore the exact reviewed spec, then approve; ledger edits are not material changes.
    spec(tmp_path, "update", "DESIGN", content=original)
    monkeypatch.setattr(owner, "request_owner_approval", lambda **kw: {"status": "approved"})
    wi(tmp_path, "request_plan_approval", work_items=[item_id], rigor="direct", spec="DESIGN")
    shown = wi(tmp_path, "show", item_id)
    assert shown["approval_projection"]["current_subject_approval"]
    spec(tmp_path, "update", "DESIGN", content=original + "A new design\n")
    shown = wi(tmp_path, "show", item_id)
    assert shown["plan_approval"]["approved"]  # Preserve the old, narrowly defined boolean.
    assert not shown["approval_projection"]["current_subject_approval"]


def test_schema_and_readiness_cli_are_discoverable_pure_and_do_not_boot(tmp_path, monkeypatch, capsys):
    from odibi_anchor import cli
    from odibi_anchor.artifacts import inspect_artifact_contracts

    monkeypatch.setattr(cli, "_boot", lambda *a: pytest.fail("inspection booted a runtime"))
    project_action(tmp_path, "create", name="alpha", output_format="dict")
    root = tmp_path / "workspace/projects/alpha"
    created = wi(root, "create", **PLAN)
    before = bytes_and_times(tmp_path)
    assert cli.main(["artifact-schema"]) == 0
    schema = json.loads(capsys.readouterr().out)["result"]
    assert schema == inspect_artifact_contracts()
    assert "scope" in schema["work_item"]["material_fields"] and schema["readiness"]["default"] == 2
    assert schema["problem"]["tables"]["hypotheses"][2] == "Hypothesis"
    assert cli.main(["readiness", "work_item", created["work_item_id"], "--home", str(tmp_path), "--project", "alpha"]) == 0
    readiness = json.loads(capsys.readouterr().out)["result"]
    assert readiness["content_addressed_for_review"] and not readiness["approval_ready"]  # No review recorded.
    assert not readiness["semantic_certification"] and not readiness["write_performed"]
    assert bytes_and_times(tmp_path) == before
    spec(root, "create", "FUTURE", content="# Future\n## Success Criteria\n- Add future.py and pass future tests\n")
    before = bytes_and_times(tmp_path)
    checked = inspect_readiness(anchor_home=tmp_path, project_id="alpha", kind="spec", identifier="FUTURE")
    assert checked["readiness"]["ready"] and not checked["readiness"]["implementation_files_checked"]
    assert bytes_and_times(tmp_path) == before


def test_intervening_ledger_write_is_never_overwritten_by_review_or_approval(tmp_path, monkeypatch):
    import odibi_anchor._dispatcher._work_item as module
    import odibi_anchor.owner_approval as owner

    item = wi(tmp_path, "create", **PLAN)
    item_id = item["work_item_id"]
    values = {"expected_fingerprint": item["fingerprint"], "verdict": "approve", "findings": [], "reviewer_ref": "claim"}
    original = module._review_subject
    intervened = False

    def interleave(record, path, **kwargs):
        nonlocal intervened
        if not intervened:
            intervened = True
            wi(tmp_path, "review", item_id, angle="concurrent", **values)
        return original(record, path, **kwargs)

    monkeypatch.setattr(module, "_review_subject", interleave)
    with pytest.raises(ValueError, match="changed before review"):
        wi(tmp_path, "review", item_id, angle="stale", **values)
    monkeypatch.setattr(module, "_review_subject", original)
    assert [r["angle"] for r in wi(tmp_path, "show", item_id)["reviews"]] == ["concurrent"]
    check = module._check_plan
    checks = 0

    def interleave_approval(*args, **kwargs):
        nonlocal checks
        checks += 1
        if checks == 2:
            wi(tmp_path, "review", item_id, angle="later", **values)
        return check(*args, **kwargs)

    monkeypatch.setattr(module, "_check_plan", interleave_approval)
    monkeypatch.setattr(owner, "request_owner_approval", lambda **kw: {"status": "approved"})
    with pytest.raises(ValueError, match="changed before approval write"):
        wi(tmp_path, "request_plan_approval", work_items=[item_id], rigor="direct")
    shown = wi(tmp_path, "show", item_id)
    assert [r["angle"] for r in shown["reviews"]] == ["concurrent", "later"] and not shown["plan_approvals"]


def test_approval_rejects_stale_preconditions_and_loss_before_owner_contact(tmp_path, monkeypatch):
    import odibi_anchor.owner_approval as owner

    decisions = []
    monkeypatch.setattr(owner, "request_owner_approval", lambda **kw: decisions.append(kw) or {"status": "approved"})
    created = wi(tmp_path, "create", **PLAN)
    item_id = created["work_item_id"]
    reviewed = wi(tmp_path, "review", item_id, angle="design", verdict="approve", findings=[], reviewer_ref="claim",
                  expected_fingerprint=created["fingerprint"])
    before = bytes_and_times(tmp_path)
    with pytest.raises(ValueError, match="stale"):
        wi(tmp_path, "request_plan_approval", work_items=[item_id], rigor="direct", write_version=2,
           expected_sha256=created["content_sha256"])
    assert not decisions and bytes_and_times(tmp_path) == before
    path = Path(reviewed["artifact_path"])
    path.write_text(path.read_text().replace("\n# WI-", "\n<!-- saved decision -->\n# WI-", 1))
    before = bytes_and_times(tmp_path)
    with pytest.raises(ValueError, match="losslessly"):
        wi(tmp_path, "request_plan_approval", work_items=[item_id], rigor="direct", write_version=2)
    assert not decisions and bytes_and_times(tmp_path) == before


@pytest.mark.parametrize("command", ["approve", "record_publish"])
def test_publication_bookkeeping_cannot_discard_saved_content_in_strict_mode(tmp_path, command):
    item = wi(tmp_path, "create", **PLAN)
    item_id = item["work_item_id"]
    values = {"provider": "synthetic", "operations": ["create_tasks"],
              "expected_fingerprint": item["fingerprint"], "approver": "caller-claim", "source": "synthetic"}
    if command == "record_publish":
        approved = wi(tmp_path, "approve", item_id, **values)
        values = {"approval_id": approved["approval_id"], "provider_id": "synthetic-1",
                  "provider_url": "https://example.invalid/task/1", "performed_operations": ["create_tasks"],
                  "recorded_by": "caller-claim"}
    path = Path(item["artifact_path"])
    path.write_text(path.read_text().replace("\n# WI-", "\n<!-- saved decision -->\n# WI-", 1))
    shown = wi(tmp_path, "show", item_id)
    before = bytes_and_times(tmp_path)
    with pytest.raises(ValueError, match="losslessly"):
        wi(tmp_path, command, item_id, write_version=2, expected_sha256=shown["content_sha256"], **values)
    assert bytes_and_times(tmp_path) == before


def test_mcp_versions_preserve_full_human_and_structured_saved_views(tmp_path, monkeypatch):
    import odibi_anchor.mcp_server as mcp

    body = "# Exact saved design\n\nNot a metadata summary.\n## Success Criteria\n- Future output\n"
    spec(tmp_path, "create", "DESIGN", content=body)

    def dispatch(action, *args, **kwargs):
        assert action == "spec"
        return spec_action(tmp_path, *args, **kwargs)

    monkeypatch.setattr(mcp, "_boot", lambda: dispatch)
    before = bytes_and_times(tmp_path)
    for version in (1, 2):
        for view in ("markdown", "dict"):
            raw = mcp.anchor_execute("spec", {"args": ["show", "DESIGN"], "output_format": view},
                                     response_version=version, response_detail="full")
            if version == 1 and view == "markdown":
                assert raw == body
                continue
            decoded = json.loads(raw)
            result = decoded["result"] if version == 2 else decoded
            assert (result["content"] if view == "dict" else result) == body
            if view == "dict":
                assert result["content_sha256"] == hashlib.sha256(body.encode()).hexdigest()
        invalid = mcp.anchor_execute("spec", {"args": ["update", "DESIGN"], "content": body,
                                             "unknown_field": "would be discarded"}, response_version=2)
        assert json.loads(invalid)["ok"] is False
    assert bytes_and_times(tmp_path) == before
