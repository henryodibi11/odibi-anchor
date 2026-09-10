"""Tests for Named Trails (directed associative sequences)."""

import os
import tempfile

import pytest

from odibi_anchor.codebase._memory_db import (
    close_db,
    get_db,
    record_trail,
    get_relevant_trails,
    get_trail_by_id,
    get_trail_stats,
)


@pytest.fixture
def tmp_db():
    tmp = tempfile.mktemp(suffix=".db")
    yield tmp
    close_db(tmp)
    if os.path.exists(tmp):
        os.unlink(tmp)


@pytest.fixture
def seeded_trails(tmp_db):
    """DB with multiple trails."""
    t1 = record_trail(tmp_db, name="schema_drift_debug",
        description="How to debug schema drift in bronze tables",
        task_goal="fix schema drift", tags=["debugging", "schema"],
        steps=[
            {"action": "profile_table", "detail": "Profile source", "outcome": "New columns found"},
            {"action": "diff", "detail": "Compare schemas", "outcome": "3 additions"},
            {"action": "transform", "detail": "Apply migration", "outcome": "Schema aligned"},
        ])
    t2 = record_trail(tmp_db, name="null_spike_investigation",
        description="Investigate sudden null spikes in sensor data",
        task_goal="diagnose null spike", tags=["debugging", "quality"],
        steps=[
            {"action": "quality", "detail": "Run quality check", "outcome": "85% nulls in temp_col"},
            {"action": "microscope", "detail": "Inspect null rows", "outcome": "All from sensor_42"},
            {"action": "case_file", "detail": "Build case file", "outcome": "Sensor offline since Tuesday"},
        ])
    t3 = record_trail(tmp_db, name="new_source_onboard",
        description="Steps to onboard a new CSV source",
        task_goal="onboard new data source", tags=["onboarding"],
        steps=[
            {"action": "profile_table", "detail": "Profile CSV", "outcome": "20 cols, 50k rows"},
            {"action": "validate", "detail": "Check quality", "outcome": "3 issues found"},
            {"action": "transform", "detail": "Clean data", "outcome": "All issues resolved"},
            {"action": "save", "detail": "Write to bronze", "outcome": "Table created"},
        ])
    return tmp_db, [t1, t2, t3]


class TestRecordTrail:
    def test_creates_trail(self, tmp_db):
        tid = record_trail(tmp_db, name="test", steps=[{"action": "a"}])
        assert tid
        assert len(tid) == 36  # UUID

    def test_empty_name_returns_empty(self, tmp_db):
        assert record_trail(tmp_db, name="", steps=[{"action": "a"}]) == ""

    def test_empty_steps_returns_empty(self, tmp_db):
        assert record_trail(tmp_db, name="test", steps=[]) == ""

    def test_stores_all_steps(self, tmp_db):
        tid = record_trail(tmp_db, name="multi",
            steps=[{"action": f"step_{i}"} for i in range(5)])
        trail = get_trail_by_id(tmp_db, trail_id=tid)
        assert len(trail["steps"]) == 5
        assert trail["steps"][2]["action"] == "step_2"

    def test_stores_tags(self, tmp_db):
        tid = record_trail(tmp_db, name="tagged", tags=["a", "b"],
            steps=[{"action": "x"}])
        trail = get_trail_by_id(tmp_db, trail_id=tid)
        assert set(trail["tags"]) == {"a", "b"}


class TestGetRelevantTrails:
    def test_keyword_search(self, seeded_trails):
        db, _ = seeded_trails
        results = get_relevant_trails(db, keyword="schema")
        assert len(results) >= 1
        assert results[0]["name"] == "schema_drift_debug"

    def test_task_goal_match(self, seeded_trails):
        db, _ = seeded_trails
        results = get_relevant_trails(db, task_goal="debug schema drift in my table")
        assert len(results) >= 1
        names = [r["name"] for r in results]
        assert "schema_drift_debug" in names

    def test_max_results(self, seeded_trails):
        db, _ = seeded_trails
        results = get_relevant_trails(db, keyword="", task_goal="debug profile", max_results=1)
        assert len(results) <= 1

    def test_no_match_returns_empty(self, seeded_trails):
        db, _ = seeded_trails
        results = get_relevant_trails(db, keyword="xyznonexistent")
        assert results == []

    def test_includes_steps(self, seeded_trails):
        db, _ = seeded_trails
        results = get_relevant_trails(db, keyword="null_spike")
        assert len(results) == 1
        assert len(results[0]["steps"]) == 3
        assert results[0]["steps"][0]["action"] == "quality"


class TestGetTrailById:
    def test_returns_trail(self, seeded_trails):
        db, ids = seeded_trails
        trail = get_trail_by_id(db, trail_id=ids[0])
        assert trail["name"] == "schema_drift_debug"

    def test_not_found_returns_none(self, tmp_db):
        get_db(tmp_db)
        assert get_trail_by_id(tmp_db, trail_id="nonexistent") is None


class TestTrailStats:
    def test_returns_stats(self, seeded_trails):
        db, _ = seeded_trails
        stats = get_trail_stats(db)
        assert stats["total_trails"] == 3
        assert stats["total_steps"] == 10  # 3 + 3 + 4

    def test_empty_db(self, tmp_db):
        get_db(tmp_db)
        stats = get_trail_stats(tmp_db)
        assert stats["total_trails"] == 0
