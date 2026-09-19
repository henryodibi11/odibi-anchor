"""Managed artifact directories must enforce their own frontmatter contract.

Issue #15: a record written directly to disk with the wrong frontmatter parsed
without complaint, persisted through snapshot, and only failed later as a raw
KeyError when a managed action read it back — putting the error surface an
arbitrary number of sessions away from its cause.

Three concerns:
  - read time names the missing fields and the call that produces a valid record;
  - snapshot reports the damage but still persists, because a preservation step
    that refuses malformed input would make the file unsnapshottable;
  - specs and decisions are advisory only. Spec readers use `.get()` defaults and
    decisions have no reader at all, so neither can fail the way problems did.
    They are reported, never enforced, so no file that works today starts failing.
"""
import pytest

from odibi_anchor._dispatcher._managed_record_schema import (
    managed_record_schemas,
    missing_required_fields,
    scan_managed_records,
)
from odibi_anchor._dispatcher._problem import _empty_record, _parse, _render

# The exact frontmatter from the issue report: `id` instead of `problem_id`,
# and none of the other canonical fields.
MALFORMED_PROBLEM = """---
id: PRB-2026-0001
title: Some problem
status: draft
---
# Content
"""


@pytest.fixture
def artifact_root(tmp_path):
    (tmp_path / "problems").mkdir()
    (tmp_path / "work_items").mkdir()
    return tmp_path


@pytest.fixture
def canonical_problem(artifact_root):
    path = artifact_root / "problems" / "PRB-2026-0002.md"
    path.write_text(
        _render(_empty_record("PRB-2026-0002", "proj", "A real problem", "full")),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def malformed_problem(artifact_root):
    path = artifact_root / "problems" / "PRB-2026-0001.md"
    path.write_text(MALFORMED_PROBLEM, encoding="utf-8")
    return path


# ── the contract is derived, not restated ────────────────────────────────────


def test_problem_schema_matches_its_writer():
    """Derived from _empty_record, so the contract cannot drift from the writer."""
    schema = managed_record_schemas()["problem"]
    written = set(_empty_record("PRB-0000-0000", "p", "t", "full")["meta"])
    assert schema.required_fields == written


def test_work_item_schema_matches_its_parser():
    from odibi_anchor._dispatcher import _work_item

    schema = managed_record_schemas()["work_item"]
    assert schema.required_fields == frozenset(_work_item.REQUIRED_META)


def test_registry_covers_all_four_managed_directories():
    assert set(managed_record_schemas()) == {"problem", "work_item", "spec", "decision"}


def test_only_problems_and_work_items_are_enforced_at_read():
    """Specs and decisions are reported, never enforced — their readers tolerate gaps."""
    from odibi_anchor._dispatcher._managed_record_schema import read_enforced_schemas

    assert set(read_enforced_schemas()) == {"problem", "work_item"}


def test_spec_schema_is_derived_from_its_scaffold():
    from odibi_anchor._dispatcher._spec import _SCAFFOLD_TEMPLATE

    required = managed_record_schemas()["spec"].required_fields
    assert required == frozenset(
        {"status", "complexity", "estimated_sessions", "phases",
         "success_criteria", "files_touched"}
    )
    for field in required:
        assert f"{field}:" in _SCAFFOLD_TEMPLATE


def test_decision_schema_is_deliberately_contentless():
    """No managed writer exists, so only a missing frontmatter block is reportable."""
    schema = managed_record_schemas()["decision"]
    assert schema.required_fields == frozenset()
    assert schema.create_call is None


# ── read time ────────────────────────────────────────────────────────────────


def test_malformed_problem_raises_valueerror_not_keyerror(malformed_problem):
    with pytest.raises(ValueError) as excinfo:
        _parse(malformed_problem)
    assert not isinstance(excinfo.value, KeyError)


def test_read_error_names_every_missing_field(malformed_problem):
    with pytest.raises(ValueError) as excinfo:
        _parse(malformed_problem)
    missing = excinfo.value.context["missing_fields"]
    assert "problem_id" in missing, "the reported case renamed problem_id to id"
    for field in ("stage", "rigor_level", "revision", "created_at", "updated_at", "next_action"):
        assert field in missing


def test_read_error_carries_a_copy_ready_repair_call(malformed_problem):
    with pytest.raises(ValueError) as excinfo:
        _parse(malformed_problem)
    assert excinfo.value.error_code == "managed_record_schema_violation"
    assert 'anchor("problem", "create"' in excinfo.value.context["repair"]
    assert excinfo.value.context["path"].endswith("PRB-2026-0001.md")


def test_canonical_record_still_parses(canonical_problem):
    """Negative control: validation must not reject what the writer produces."""
    parsed = _parse(canonical_problem)
    assert parsed["meta"]["problem_id"] == "PRB-2026-0002"
    assert parsed["meta"]["stage"] == 1


def test_work_item_missing_frontmatter_message_offers_the_create_call(artifact_root):
    from odibi_anchor._dispatcher._work_item import _parse as parse_work_item

    path = artifact_root / "work_items" / "WI-2026-0001.md"
    path.write_text('---\nwork_item_id: "WI-2026-0001"\n---\n# x\n', encoding="utf-8")
    with pytest.raises(ValueError) as excinfo:
        parse_work_item(path)
    assert 'anchor("work_item", "create"' in str(excinfo.value)


# ── scanning ─────────────────────────────────────────────────────────────────


def test_scan_reports_the_malformed_record(malformed_problem):
    findings = scan_managed_records(malformed_problem.parent.parent)
    assert len(findings) == 1
    assert findings[0]["record_kind"] == "problem"
    assert findings[0]["path"] == str(malformed_problem)


def test_scan_is_silent_on_canonical_records(canonical_problem):
    assert scan_managed_records(canonical_problem.parent.parent) == []


def test_scan_reports_a_file_with_no_frontmatter_block(artifact_root):
    path = artifact_root / "problems" / "PRB-2026-0003.md"
    path.write_text("# No frontmatter at all\n", encoding="utf-8")
    findings = scan_managed_records(artifact_root)
    assert len(findings) == 1
    assert findings[0]["missing_fields"]


def test_scan_never_raises_on_unreadable_input(artifact_root):
    """The scanner describes broken files, so it must not break on them."""
    (artifact_root / "problems" / "PRB-2026-0004.md").write_bytes(b"\xff\xfe not utf-8")
    (artifact_root / "problems" / "PRB-2026-0005.md").write_text("---\nbroken", encoding="utf-8")
    findings = scan_managed_records(artifact_root)
    assert len(findings) == 2


def test_scan_ignores_directories_that_do_not_exist(tmp_path):
    assert scan_managed_records(tmp_path / "nowhere") == []


def test_missing_required_fields_is_empty_for_a_complete_record():
    schema = managed_record_schemas()["problem"]
    complete = _empty_record("PRB-0000-0000", "p", "t", "full")["meta"]
    assert missing_required_fields(complete, schema) == []


# ── listing reports per file and keeps going ─────────────────────────────────


@pytest.fixture
def mixed_problems(artifact_root, canonical_problem, malformed_problem):
    """One canonical record and one written directly to disk."""
    return artifact_root


def _list_problems(artifact_root):
    from odibi_anchor._dispatcher._problem import problem_action

    return problem_action(
        artifact_root=str(artifact_root), project_id="proj",
        args=("list",), output_format="dict",
    )


def test_one_malformed_record_does_not_hide_the_valid_ones(mixed_problems):
    """The reported failure mode aborted the whole listing."""
    result = _list_problems(mixed_problems)
    assert result["count"] == 1
    assert result["problems"][0]["problem_id"] == "PRB-2026-0002"


def test_listing_reports_the_malformed_record_per_file(mixed_problems):
    result = _list_problems(mixed_problems)
    assert result["malformed_count"] == 1
    entry = result["malformed_records"][0]
    assert entry["path"].endswith("PRB-2026-0001.md")
    assert "problem_id" in entry["missing_fields"]
    assert 'anchor("problem", "create"' in entry["repair"]


def test_listing_points_at_the_repair(mixed_problems):
    result = _list_problems(mixed_problems)
    assert any("Repair 1 malformed" in action for action in result["suggested_next_actions"])


def test_listing_a_clean_directory_reports_no_malformed_records(artifact_root, canonical_problem):
    """Negative control: the new fields must stay empty when nothing is wrong."""
    result = _list_problems(artifact_root)
    assert result["malformed_count"] == 0
    assert result["malformed_records"] == []
    assert result["count"] == 1


# ── advisory tier: reported at snapshot, never enforced at read ──────────────


@pytest.fixture
def advisory_root(tmp_path):
    (tmp_path / "specs").mkdir()
    (tmp_path / "decisions").mkdir()
    (tmp_path / "specs" / "THING_SPEC.md").write_text(
        "---\nstatus: draft\n---\n# Thing Specification\n", encoding="utf-8",
    )
    (tmp_path / "decisions" / "D-001.md").write_text(
        "# A decision with no frontmatter\n", encoding="utf-8",
    )
    return tmp_path


def test_sparse_spec_is_reported_as_advisory(advisory_root):
    spec = [f for f in scan_managed_records(advisory_root) if f["record_kind"] == "spec"]
    assert len(spec) == 1
    assert spec[0]["advisory"] is True
    assert spec[0]["enforcement"] == "advisory"
    assert "complexity" in spec[0]["missing_fields"]


def test_decision_without_frontmatter_is_reported_as_advisory(advisory_root):
    found = [f for f in scan_managed_records(advisory_root) if f["record_kind"] == "decision"]
    assert len(found) == 1
    assert found[0]["advisory"] is True
    assert "no readable decision frontmatter" in found[0]["reason"]


def test_decision_repair_does_not_invent_a_managed_call(advisory_root):
    """There is no decision action, so pointing at one would be a lie."""
    found = [f for f in scan_managed_records(advisory_root) if f["record_kind"] == "decision"]
    assert "anchor(" not in found[0]["repair"]
    assert "convention" in found[0]["repair"]


def test_a_decision_with_any_frontmatter_is_accepted(advisory_root):
    """Contentless contract: presence is all that can honestly be checked."""
    (advisory_root / "decisions" / "D-002.md").write_text(
        "---\nanything: at all\n---\n# ok\n", encoding="utf-8",
    )
    kinds = [f["path"] for f in scan_managed_records(advisory_root)]
    assert not any(path.endswith("D-002.md") for path in kinds)


def test_enforced_findings_are_distinguishable_from_advisory(artifact_root, malformed_problem):
    (artifact_root / "specs").mkdir()
    (artifact_root / "specs" / "X_SPEC.md").write_text(
        "---\nstatus: draft\n---\n# X\n", encoding="utf-8",
    )
    findings = scan_managed_records(artifact_root)
    by_kind = {f["record_kind"]: f for f in findings}
    assert by_kind["problem"]["advisory"] is False
    assert by_kind["problem"]["enforcement"] == "read_enforced"
    assert by_kind["spec"]["advisory"] is True


def test_spec_reads_are_unaffected_by_the_advisory_entry(advisory_root):
    """The whole point of advisory: no new rejection path."""
    from odibi_anchor.codebase._spec_parser import _parse_frontmatter

    sparse = (advisory_root / "specs" / "THING_SPEC.md").read_text(encoding="utf-8")
    assert _parse_frontmatter(sparse) == {"status": "draft"}
    assert _parse_frontmatter("# no block at all\n") is None


def test_a_canonical_spec_produces_no_finding(advisory_root):
    from odibi_anchor._dispatcher._spec import _SCAFFOLD_TEMPLATE

    (advisory_root / "specs" / "GOOD_SPEC.md").write_text(
        _SCAFFOLD_TEMPLATE.format(title="Good"), encoding="utf-8",
    )
    paths = [f["path"] for f in scan_managed_records(advisory_root)]
    assert not any(path.endswith("GOOD_SPEC.md") for path in paths)
