"""Tests for Marginalia (contextual location-bound annotations)."""

import os
import tempfile

import pytest

from odibi_anchor.codebase._memory_db import (
    close_db,
    get_db,
    annotate_location,
    get_annotations_for_file,
    get_annotations_for_table,
    get_annotation_stats,
)


@pytest.fixture
def tmp_db():
    tmp = tempfile.mktemp(suffix=".db")
    yield tmp
    close_db(tmp)
    if os.path.exists(tmp):
        os.unlink(tmp)


@pytest.fixture
def seeded_annotations(tmp_db):
    """DB with various annotations."""
    a1 = annotate_location(tmp_db, content="Race condition on concurrent writes",
        location_type="file", file_path="/src/pipeline/writer.py", line_start=45, line_end=52)
    a2 = annotate_location(tmp_db, content="Config parsing is fragile here",
        location_type="file", file_path="/src/pipeline/writer.py", line_start=100, line_end=110)
    a3 = annotate_location(tmp_db, content="General note about this file",
        location_type="file", file_path="/src/pipeline/writer.py")
    a4 = annotate_location(tmp_db, content="epoch seconds not milliseconds",
        location_type="column", table_name="bronze.sensors.readings", column_name="timestamp_col",
        tags=["gotcha", "time"])
    a5 = annotate_location(tmp_db, content="Can contain negative values (valid)",
        location_type="column", table_name="bronze.sensors.readings", column_name="temperature")
    a6 = annotate_location(tmp_db, content="Refreshes daily at 3am UTC",
        location_type="table", table_name="bronze.sensors.readings")
    return tmp_db, [a1, a2, a3, a4, a5, a6]


class TestAnnotateLocation:
    def test_creates_file_annotation(self, tmp_db):
        aid = annotate_location(tmp_db, content="test note",
            location_type="file", file_path="/src/test.py")
        assert aid and len(aid) == 36

    def test_creates_table_annotation(self, tmp_db):
        aid = annotate_location(tmp_db, content="table note",
            location_type="table", table_name="catalog.schema.table")
        assert aid

    def test_creates_column_annotation(self, tmp_db):
        aid = annotate_location(tmp_db, content="col note",
            location_type="column", table_name="catalog.schema.table", column_name="col1")
        assert aid

    def test_rejects_invalid_type(self, tmp_db):
        assert annotate_location(tmp_db, content="x", location_type="invalid") == ""

    def test_rejects_file_without_path(self, tmp_db):
        assert annotate_location(tmp_db, content="x", location_type="file") == ""

    def test_rejects_table_without_name(self, tmp_db):
        assert annotate_location(tmp_db, content="x", location_type="table") == ""

    def test_rejects_column_without_column_name(self, tmp_db):
        assert annotate_location(tmp_db, content="x", location_type="column", table_name="t") == ""

    def test_rejects_empty_content(self, tmp_db):
        assert annotate_location(tmp_db, content="", location_type="file", file_path="/x") == ""


class TestGetAnnotationsForFile:
    def test_returns_all_for_file(self, seeded_annotations):
        db, _ = seeded_annotations
        anns = get_annotations_for_file(db, file_path="/src/pipeline/writer.py")
        assert len(anns) == 3

    def test_line_filter_hit(self, seeded_annotations):
        db, _ = seeded_annotations
        anns = get_annotations_for_file(db, file_path="/src/pipeline/writer.py", line=48)
        # Should get: line 45-52 annotation + the one with no line range
        assert len(anns) == 2
        contents = [a["content"] for a in anns]
        assert "Race condition" in contents[0] or "Race condition" in contents[1]

    def test_line_filter_miss(self, seeded_annotations):
        db, _ = seeded_annotations
        anns = get_annotations_for_file(db, file_path="/src/pipeline/writer.py", line=75)
        # Only the annotation with no line range matches
        assert len(anns) == 1
        assert "General note" in anns[0]["content"]

    def test_nonexistent_file(self, seeded_annotations):
        db, _ = seeded_annotations
        assert get_annotations_for_file(db, file_path="/nonexistent.py") == []

    def test_empty_path_returns_empty(self, tmp_db):
        assert get_annotations_for_file(tmp_db, file_path="") == []


class TestGetAnnotationsForTable:
    def test_returns_all_for_table(self, seeded_annotations):
        db, _ = seeded_annotations
        anns = get_annotations_for_table(db, table_name="bronze.sensors.readings")
        assert len(anns) == 3  # 1 table + 2 column annotations

    def test_column_filter(self, seeded_annotations):
        db, _ = seeded_annotations
        anns = get_annotations_for_table(db, table_name="bronze.sensors.readings", column_name="timestamp_col")
        assert len(anns) == 1
        assert "epoch seconds" in anns[0]["content"]

    def test_nonexistent_table(self, seeded_annotations):
        db, _ = seeded_annotations
        assert get_annotations_for_table(db, table_name="nonexistent") == []

    def test_tags_preserved(self, seeded_annotations):
        db, _ = seeded_annotations
        anns = get_annotations_for_table(db, table_name="bronze.sensors.readings", column_name="timestamp_col")
        assert set(anns[0]["tags"]) == {"gotcha", "time"}


class TestAnnotationStats:
    def test_returns_stats(self, seeded_annotations):
        db, _ = seeded_annotations
        stats = get_annotation_stats(db)
        assert stats["total_annotations"] == 6
        assert stats["by_type"]["file"] == 3
        assert stats["by_type"]["column"] == 2
        assert stats["by_type"]["table"] == 1

    def test_empty_db(self, tmp_db):
        get_db(tmp_db)
        stats = get_annotation_stats(tmp_db)
        assert stats["total_annotations"] == 0
