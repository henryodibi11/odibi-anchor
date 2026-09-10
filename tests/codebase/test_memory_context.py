"""Tests for memory_context (SQLite-backed)."""

import json
import os
import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from odibi_anchor.codebase.memory_context import (
    memory_context,
    render_memory_report,
    append_memory,
    archive_stale_entries,
    export_markdown,
    import_from_markdown,
    _score_entries,
    _extract_tags_from_content,
)
from odibi_anchor.codebase._memory_db import (
    get_db, close_db, insert_memory as _db_insert, query_memories as _db_query,
)
from odibi_anchor.codebase._memory_lifecycle import (
    evaluate_application, record_application, record_selection,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def memory_root(tmp_path):
    """Create a project root with populated SQLite memory. Returns (path, db_path)."""
    db = str(tmp_path / "test_memory.db")
    entries = [
        {"project": "odibi_anchor", "type": "gotcha",
         "content": "pytest collects test_ prefixed source files",
         "related_files": ["src/**/focus_context.py"], "tags": ["pytest", "naming"],
         "confidence": 0.8, "status": "confirmed"},
        {"project": "odibi_anchor", "type": "decision",
         "content": "Extracted _utils for shared logic",
         "related_files": ["src/**/_utils/*.py"], "tags": ["refactor", "v0.3.0"],
         "confidence": 0.7, "status": "active"},
        {"project": "odibi_anchor", "type": "pattern",
         "content": "Every tool returns standard contract dict",
         "related_files": ["src/**/*_context.py"], "tags": ["contract", "testing"],
         "confidence": 0.9, "status": "confirmed"},
        {"project": "odibi_anchor", "type": "gotcha",
         "content": "importlib.reload does not cascade in notebooks",
         "related_files": [], "tags": ["notebook", "imports"],
         "confidence": 0.5, "status": "candidate"},
        {"project": "odibi_anchor", "type": "gotcha",
         "content": "Unity Catalog naming requires three-part names",
         "related_files": [], "tags": ["spark", "naming", "data"],
         "confidence": 0.5, "status": "candidate"},
    ]
    for e in entries:
        _db_insert(
            db,
            project=e["project"],
            type=e["type"],
            content=e["content"],
            related_files=e.get("related_files"),
            tags=e.get("tags"),
            confidence=e.get("confidence", 0.5),
            status=e.get("status", "candidate"),
        )
    yield (tmp_path, db)
    close_db(db)


# ---------------------------------------------------------------------------
# Standard Contract
# ---------------------------------------------------------------------------


class TestStandardContract:
    def test_returns_dict_by_default(self, memory_root):
        ctx = memory_context(str(memory_root[0]), db_path=memory_root[1], project="odibi_anchor")
        assert isinstance(ctx, dict)

    def test_has_all_standard_fields(self, memory_root):
        ctx = memory_context(str(memory_root[0]), db_path=memory_root[1], project="odibi_anchor")
        required = {"kind", "subject", "summary", "metrics", "findings",
                     "risks", "samples", "suggested_next_actions"}
        assert required.issubset(ctx.keys())

    def test_kind_is_correct(self, memory_root):
        ctx = memory_context(str(memory_root[0]), db_path=memory_root[1], project="odibi_anchor")
        assert ctx["kind"] == "memory_context"

    def test_json_serializable(self, memory_root):
        ctx = memory_context(str(memory_root[0]), files_involved=["src/foo.py"],
                             db_path=memory_root[1], project="odibi_anchor")
        json.dumps(ctx)

    def test_markdown_output(self, memory_root):
        md = memory_context(str(memory_root[0]), output_format="markdown",
                            db_path=memory_root[1], project="odibi_anchor")
        assert isinstance(md, str)
        assert "Memory" in md


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------


class TestRetrieval:
    def test_file_matching(self, memory_root):
        """Entries with matching related_files score highest."""
        ctx = memory_context(
            str(memory_root[0]),
            files_involved=["src/odibi_anchor/_utils/contract.py"],
            db_path=memory_root[1], project="odibi_anchor",
        )
        assert ctx["metrics"]["has_results"] is True
        contents = [e["content"] for e in ctx["entries"]]
        # Should find _utils or *_context.py entries
        assert any("_utils" in c or "contract" in c for c in contents)

    def test_tag_matching(self, memory_root):
        """Entries matching provided tags are returned."""
        ctx = memory_context(str(memory_root[0]), tags=["pytest"],
                             db_path=memory_root[1], project="odibi_anchor")
        contents = [e["content"] for e in ctx["entries"]]
        assert any("pytest" in c for c in contents)

    def test_error_text_matching(self, memory_root):
        """Gotchas matching error text are surfaced."""
        ctx = memory_context(
            str(memory_root[0]),
            error_text="fixture 'root' not found, pytest collects source files",
            db_path=memory_root[1], project="odibi_anchor",
        )
        contents = [e["content"] for e in ctx["entries"]]
        assert any("pytest" in c for c in contents)

    def test_query_matching(self, memory_root):
        """Free-text query finds relevant entries."""
        ctx = memory_context(str(memory_root[0]), query="contract dict",
                             db_path=memory_root[1], project="odibi_anchor")
        contents = [e["content"] for e in ctx["entries"]]
        assert any("contract" in c for c in contents)

    def test_entry_type_filter(self, memory_root):
        """entry_type filters to specific type."""
        ctx = memory_context(str(memory_root[0]), entry_type="decision",
                             db_path=memory_root[1], project="odibi_anchor")
        assert all(e["type"] == "decision" for e in ctx["entries"])

    def test_limit_respected(self, memory_root):
        ctx = memory_context(str(memory_root[0]), query="", limit=2,
                             tags=["naming"], db_path=memory_root[1], project="odibi_anchor")
        assert len(ctx["entries"]) <= 2

    def test_pagination_slices_final_ranked_filtered_results(self, memory_root):
        all_results = memory_context(
            str(memory_root[0]), tags=["naming"], limit=10,
            db_path=memory_root[1], project="odibi_anchor",
        )
        first = memory_context(
            str(memory_root[0]), tags=["naming"], limit=1, offset=0,
            db_path=memory_root[1], project="odibi_anchor",
        )
        second = memory_context(
            str(memory_root[0]), tags=["naming"], limit=1, offset=1,
            db_path=memory_root[1], project="odibi_anchor",
        )

        expected_ids = [entry["id"] for entry in all_results["entries"]]
        assert [first["entries"][0]["id"], second["entries"][0]["id"]] == expected_ids[:2]
        expected_pagination = {
            "total_matches": len(expected_ids), "returned_count": 1,
            "offset": 0, "next_offset": 1, "has_more": True,
        }
        assert {key: first["metrics"][key] for key in expected_pagination} == expected_pagination
        assert second["metrics"]["offset"] == 1

    def test_pagination_reports_terminal_and_empty_pages(self, memory_root):
        matches = memory_context(
            str(memory_root[0]), tags=["naming"], limit=100,
            db_path=memory_root[1], project="odibi_anchor",
        )["metrics"]["total_matches"]
        terminal = memory_context(
            str(memory_root[0]), tags=["naming"], limit=1, offset=matches - 1,
            db_path=memory_root[1], project="odibi_anchor",
        )
        empty = memory_context(
            str(memory_root[0]), tags=["naming"], limit=1, offset=matches,
            db_path=memory_root[1], project="odibi_anchor",
        )

        assert terminal["metrics"]["returned_count"] == 1
        assert terminal["metrics"]["next_offset"] is None
        assert terminal["metrics"]["has_more"] is False
        assert empty["entries"] == []
        assert empty["metrics"]["returned_count"] == 0
        assert empty["metrics"]["offset"] == matches
        assert empty["metrics"]["next_offset"] is None
        assert empty["metrics"]["has_more"] is False

    @pytest.mark.parametrize(
        ("argument", "value", "error"),
        [("limit", True, TypeError), ("limit", 0, ValueError),
         ("offset", False, TypeError), ("offset", -1, ValueError)],
    )
    def test_pagination_rejects_invalid_bounds(self, memory_root, argument, value, error):
        with pytest.raises(error):
            memory_context(
                str(memory_root[0]), db_path=memory_root[1], project="odibi_anchor",
                **{argument: value},
            )

    def test_no_results(self, memory_root):
        ctx = memory_context(
            str(memory_root[0]),
            files_involved=["totally/unrelated/file.rs"],
            db_path=memory_root[1], project="odibi_anchor",
        )
        assert ctx["metrics"]["returned_entries"] >= 0

    def test_empty_memory(self, tmp_path):
        """Works with no entries in DB."""
        db = str(tmp_path / "empty.db")
        ctx = memory_context(str(tmp_path), db_path=db)
        assert ctx["metrics"]["total_entries"] == 0
        assert ctx["metrics"]["has_results"] is False
        close_db(db)

    def test_immutable_helpful_evidence_affects_rank_not_authority(self, tmp_path):
        db = str(tmp_path / "rank.db")
        helpful = _db_insert(
            db, project="project:test", type="pattern",
            content="Use exact cohort rates for memory diagnostics.",
        )
        newer = _db_insert(
            db, project="project:test", type="pattern",
            content="Use exact cohort labels for memory diagnostics.",
        )
        selection = record_selection(
            db, task_window_id="ltw-1", query={"query": "cohort diagnostics"},
            memory_id=helpful["id"], reason={"match": "query"},
        )
        application = record_application(
            db, selection_id=selection["selection_id"], task_window_id="ltw-1",
            action="apply exact cohort rates",
        )
        evaluate_application(
            db, application_id=application["application_id"], task_window_id="ltw-1",
            outcome="helpful", evidence={"test": "diagnostic reconciliation passed"},
        )
        get_db(db).execute(
            "UPDATE memories SET applied_count=999,confirmation_count=999 WHERE id=?",
            (newer["id"],),
        )
        get_db(db).commit()

        result = memory_context(
            tmp_path, project="project:test", query="cohort memory diagnostics",
            db_path=db, limit=2,
        )
        assert [entry["id"] for entry in result["entries"]] == [helpful["id"], newer["id"]]
        assert {entry["status"] for entry in result["entries"]} == {"candidate"}
        assert {entry["confidence"] for entry in result["entries"]} == {0.5}


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------


class TestAppendMemory:
    def test_append_creates_entry(self, tmp_path):
        db = str(tmp_path / "test.db")
        entry = append_memory(
            str(tmp_path),
            entry_type="gotcha",
            content="Test gotcha entry",
            tags=["test"],
            db_path=db,
        )
        assert entry["action"] == "inserted"
        assert entry["id"]  # UUID string
        assert entry["content"] == "Test gotcha entry"

        # Verify it's in DB
        results = _db_query(db, query="gotcha entry")
        assert len(results) >= 1
        close_db(db)

    def test_append_dedup(self, tmp_path):
        """Inserting same content twice deduplicates."""
        db = str(tmp_path / "test.db")
        r1 = append_memory(str(tmp_path), entry_type="gotcha",
                           content="Same content", db_path=db)
        r2 = append_memory(str(tmp_path), entry_type="gotcha",
                           content="Same content", db_path=db)
        assert r1["action"] == "inserted"
        assert r2["action"] == "deduped"
        close_db(db)

    def test_append_invalid_type_raises(self, tmp_path):
        db = str(tmp_path / "test.db")
        with pytest.raises(ValueError, match="Invalid type"):
            append_memory(str(tmp_path), entry_type="invalid",
                          content="test", db_path=db)
        close_db(db)

    @pytest.mark.parametrize("confidence", [float("nan"), float("inf"), -0.1, 1.1, True, "0.5"])
    def test_append_inherits_fail_closed_confidence_validation(self, tmp_path, confidence):
        db = str(tmp_path / "test.db")
        with pytest.raises(ValueError, match="finite real number"):
            append_memory(
                str(tmp_path), entry_type="gotcha", content="Invalid confidence",
                confidence=confidence, db_path=db,
            )
        assert not Path(db).exists()

    def test_append_with_related_files(self, tmp_path):
        db = str(tmp_path / "test.db")
        entry = append_memory(
            str(tmp_path),
            entry_type="gotcha",
            content="Contract requires non-empty summary",
            related_files=["src/**/_utils/contract.py"],
            tags=["contract"],
            db_path=db,
        )
        assert entry["action"] == "inserted"
        # Verify via query
        results = _db_query(db, query="Contract requires")
        assert len(results) >= 1
        assert results[0]["related_files"] == ["src/**/_utils/contract.py"]
        close_db(db)

    def test_append_cross_project(self, tmp_path):
        """project='all' creates cross-cutting entries."""
        db = str(tmp_path / "test.db")
        entry = append_memory(
            str(tmp_path),
            entry_type="gotcha",
            content="Cross-cutting knowledge",
            project="all",
            db_path=db,
        )
        assert entry["action"] == "inserted"
        # Should be findable from any project
        results = _db_query(db, project="odibi", query="Cross-cutting")
        assert len(results) >= 1
        close_db(db)


# ---------------------------------------------------------------------------
# Archive
# ---------------------------------------------------------------------------


class TestArchive:
    def test_archive_stale(self, memory_root):
        result = archive_stale_entries(str(memory_root[0]), max_unused_days=0,
                                       db_path=memory_root[1], project="odibi_anchor")
        # candidate entries (not confirmed) should be archived
        assert result["archived_count"] >= 0

    def test_archive_preserves_recent(self, tmp_path):
        db = str(tmp_path / "test.db")
        append_memory(str(tmp_path), entry_type="gotcha",
                      content="Fresh entry", db_path=db)
        result = archive_stale_entries(str(tmp_path), max_unused_days=30,
                                       db_path=db)
        assert result["archived_count"] == 0
        close_db(db)


# ---------------------------------------------------------------------------
# Export / Import
# ---------------------------------------------------------------------------


class TestExportImport:
    def test_export_markdown(self, memory_root):
        md = export_markdown(str(memory_root[0]), db_path=memory_root[1], project="odibi_anchor")
        assert "## Gotchas" in md
        assert "pytest" in md

    def test_import_from_markdown(self, tmp_path):
        db = str(tmp_path / "test.db")
        md_content = """# Agent Memory \u2014 odibi_anchor

## Gotchas
- **pytest collection**: Source file starts with test_ \u2014 pytest will collect it.
- **module reload**: importlib.reload() doesn't cascade.

## Patterns
- **Standard contract**: Every tool returns dict with: kind, subject, summary.

## Decisions
- **v0.3.0 refactor**: Extracted _utils for shared logic.
"""
        md_path = tmp_path / ".agent_memory.md"
        md_path.write_text(md_content, encoding="utf-8")

        result = import_from_markdown(str(tmp_path), db_path=db)
        assert result["imported_count"] == 4

        # Verify entries in DB
        results = _db_query(db)
        assert len(results) >= 4
        assert {entry["status"] for entry in results} == {"candidate"}
        assert {entry["confidence"] for entry in results} == {0.5}
        types = {r["type"] for r in results}
        assert "gotcha" in types
        assert "pattern" in types
        assert "decision" in types
        close_db(db)

    def test_import_nonexistent(self, tmp_path):
        db = str(tmp_path / "test.db")
        result = import_from_markdown(str(tmp_path), db_path=db)
        assert result["imported_count"] == 0
        close_db(db)

    def test_import_dedup(self, tmp_path):
        """Importing same file twice deduplicates."""
        db = str(tmp_path / "test.db")
        md_content = """# Memory

## Gotchas
- Same entry twice
"""
        md_path = tmp_path / ".agent_memory.md"
        md_path.write_text(md_content)

        r1 = import_from_markdown(str(tmp_path), db_path=db)
        r2 = import_from_markdown(str(tmp_path), db_path=db)
        assert r1["imported_count"] == 1
        assert r2["imported_count"] == 0
        assert r2["skipped_count"] == 1
        close_db(db)


# ---------------------------------------------------------------------------
# Scoring internals
# ---------------------------------------------------------------------------


class TestScoring:
    def test_score_entries_returns_sorted(self, memory_root):
        entries = _db_query(memory_root[1], project="odibi_anchor")
        scored = _score_entries(
            entries,
            files_involved=[],
            tags=["pytest"],
            error_text="",
            task_type="",
            query="",
        )
        # Entry with pytest tag should be first
        assert "pytest" in scored[0].get("content", "").lower() or "pytest" in scored[0].get("tags", [])

    def test_extract_tags_from_content(self):
        tags = _extract_tags_from_content("pytest fixture conftest import")
        assert "pytest" in tags
        assert "imports" in tags


# ---------------------------------------------------------------------------
# Regression: Scoring baseline fix (v0.4.0)
# ---------------------------------------------------------------------------


class TestScoringBaselineFix:
    """Regression tests for browse mode (no filters returns all entries)."""

    def test_no_filter_returns_all_entries(self, memory_root):
        ctx = memory_context(str(memory_root[0]), db_path=memory_root[1], project="odibi_anchor")
        assert ctx["metrics"]["returned_entries"] == 5
        assert ctx["metrics"]["has_results"] is True

    def test_entry_type_only_returns_matching(self, memory_root):
        ctx = memory_context(str(memory_root[0]), entry_type="decision",
                             db_path=memory_root[1], project="odibi_anchor")
        assert ctx["metrics"]["returned_entries"] == 1
        assert ctx["entries"][0]["type"] == "decision"

    def test_entry_type_gotcha_returns_all_gotchas(self, memory_root):
        ctx = memory_context(str(memory_root[0]), entry_type="gotcha",
                             db_path=memory_root[1], project="odibi_anchor")
        assert ctx["metrics"]["returned_entries"] == 3
        assert all(e["type"] == "gotcha" for e in ctx["entries"])

    def test_entry_type_with_no_matches_returns_zero(self, memory_root):
        ctx = memory_context(str(memory_root[0]), entry_type="failure_pattern",
                             db_path=memory_root[1], project="odibi_anchor")
        assert ctx["metrics"]["returned_entries"] == 0
        assert ctx["metrics"]["has_results"] is False

    def test_no_filter_entries_respect_limit(self, memory_root):
        ctx = memory_context(str(memory_root[0]), limit=2,
                             db_path=memory_root[1], project="odibi_anchor")
        assert ctx["metrics"]["returned_entries"] == 2

    def test_signals_still_filter_correctly(self, memory_root):
        ctx = memory_context(str(memory_root[0]), query="Unity Catalog naming",
                             db_path=memory_root[1], project="odibi_anchor")
        contents = [e["content"] for e in ctx["entries"]]
        assert any("Unity Catalog" in c for c in contents)

    def test_score_entries_browse_mode_directly(self, memory_root):
        entries = _db_query(memory_root[1], project="odibi_anchor")
        scored = _score_entries(
            entries,
            files_involved=[],
            tags=[],
            error_text="",
            task_type="",
            query="",
        )
        assert len(scored) == 5

    def test_score_entries_with_signal_filters_zero_score(self, memory_root):
        entries = _db_query(memory_root[1], project="odibi_anchor")
        scored = _score_entries(
            entries,
            files_involved=["totally/unique/path.rs"],
            tags=["nonexistent_tag_xyz"],
            error_text="",
            task_type="",
            query="",
        )
        assert len(scored) < 5
