"""Managed artifact directories must enforce their own frontmatter contract.

Issue #15: a record written directly to disk with the wrong frontmatter parsed
without complaint, persisted through snapshot, and only failed later as a raw
KeyError when a managed action read it back — putting the error surface an
arbitrary number of sessions away from its cause.

Two halves:
  - read time names the missing fields and the call that produces a valid record;
  - snapshot reports the damage but still persists, because a preservation step
    that refuses malformed input would make the file unsnapshottable.
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


def test_registry_covers_only_types_with_canonical_frontmatter():
    """Specs are free-form and decisions have no managed action; neither has a contract."""
    assert set(managed_record_schemas()) == {"problem", "work_item"}


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
