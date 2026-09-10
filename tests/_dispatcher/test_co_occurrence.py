"""Tests for co-occurrence linking (associative memory retrieval)."""

import os
import tempfile

import pytest

from odibi_anchor.codebase._memory_db import (
    close_db,
    get_db,
    record_memory_load,
    compute_co_occurrences,
    get_associations,
    get_co_occurrence_stats,
    insert_memory,
)


@pytest.fixture
def tmp_db():
    """Create a temporary database for testing."""
    tmp = tempfile.mktemp(suffix=".db")
    yield tmp
    close_db(tmp)
    if os.path.exists(tmp):
        os.unlink(tmp)


@pytest.fixture
def seeded_db(tmp_db):
    """DB with memories and 3 sessions of co-occurrence data."""
    for mid in ["m1", "m2", "m3", "m4", "m5"]:
        insert_memory(
            tmp_db,
            project="test",
            type="gotcha",
            content=f"Memory content for {mid} - this is a test entry with enough content",
        )
    conn = get_db(tmp_db)
    rows = conn.execute("SELECT id FROM memories ORDER BY created").fetchall()
    ids = [row[0] for row in rows]
    for sid in ["session_1", "session_2", "session_3"]:
        record_memory_load(tmp_db, session_id=sid, memory_ids=ids[:3])
        compute_co_occurrences(tmp_db, session_id=sid, session_score=12, max_score=15)
    return tmp_db, ids


class TestRecordMemoryLoad:
    def test_records_loads(self, tmp_db):
        n = record_memory_load(tmp_db, session_id="s1", memory_ids=["a", "b", "c"])
        assert n == 3

    def test_empty_memory_ids_returns_zero(self, tmp_db):
        assert record_memory_load(tmp_db, session_id="s1", memory_ids=[]) == 0

    def test_empty_session_id_returns_zero(self, tmp_db):
        assert record_memory_load(tmp_db, session_id="", memory_ids=["a"]) == 0

    def test_duplicate_inserts_are_idempotent(self, tmp_db):
        record_memory_load(tmp_db, session_id="s1", memory_ids=["a", "b"])
        record_memory_load(tmp_db, session_id="s1", memory_ids=["a", "b"])
        conn = get_db(tmp_db)
        count = conn.execute("SELECT COUNT(*) FROM session_memory_loads").fetchone()[0]
        assert count == 2


class TestComputeCoOccurrences:
    def test_computes_pairs(self, tmp_db):
        record_memory_load(tmp_db, session_id="s1", memory_ids=["a", "b", "c"])
        pairs = compute_co_occurrences(tmp_db, session_id="s1", session_score=10, max_score=15)
        assert pairs == 3

    def test_low_score_skips(self, tmp_db):
        record_memory_load(tmp_db, session_id="s1", memory_ids=["a", "b", "c"])
        pairs = compute_co_occurrences(tmp_db, session_id="s1", session_score=3, max_score=15)
        assert pairs == 0

    def test_single_memory_no_pairs(self, tmp_db):
        record_memory_load(tmp_db, session_id="s1", memory_ids=["a"])
        pairs = compute_co_occurrences(tmp_db, session_id="s1", session_score=10, max_score=15)
        assert pairs == 0

    def test_empty_session_returns_zero(self, tmp_db):
        assert compute_co_occurrences(tmp_db, session_id="", session_score=10, max_score=15) == 0

    def test_increments_count_on_repeat(self, tmp_db):
        record_memory_load(tmp_db, session_id="s1", memory_ids=["a", "b"])
        compute_co_occurrences(tmp_db, session_id="s1", session_score=10, max_score=15)
        record_memory_load(tmp_db, session_id="s2", memory_ids=["a", "b"])
        compute_co_occurrences(tmp_db, session_id="s2", session_score=10, max_score=15)
        conn = get_db(tmp_db)
        row = conn.execute(
            "SELECT co_occurrence_count FROM memory_co_occurrences WHERE memory_id_a = ? AND memory_id_b = ?",
            ("a", "b"),
        ).fetchone()
        assert row[0] == 2

    def test_canonical_ordering(self, tmp_db):
        record_memory_load(tmp_db, session_id="s1", memory_ids=["z", "a", "m"])
        compute_co_occurrences(tmp_db, session_id="s1", session_score=10, max_score=15)
        conn = get_db(tmp_db)
        rows = conn.execute("SELECT memory_id_a, memory_id_b FROM memory_co_occurrences").fetchall()
        for row in rows:
            assert row[0] < row[1]


class TestGetAssociations:
    def test_returns_associations_after_threshold(self, seeded_db):
        tmp_db, ids = seeded_db
        assocs = get_associations(tmp_db, memory_ids=[ids[0]])
        assert len(assocs) > 0
        assoc_ids = {a["memory_id"] for a in assocs}
        assert ids[1] in assoc_ids or ids[2] in assoc_ids

    def test_respects_min_co_occurrences(self, tmp_db):
        record_memory_load(tmp_db, session_id="s1", memory_ids=["a", "b"])
        compute_co_occurrences(tmp_db, session_id="s1", session_score=10, max_score=15)
        assocs = get_associations(tmp_db, memory_ids=["a"])
        assert assocs == []

    def test_exclude_ids(self, seeded_db):
        tmp_db, ids = seeded_db
        assocs = get_associations(tmp_db, memory_ids=[ids[0]], exclude_ids=[ids[1], ids[2]])
        assoc_ids = {a["memory_id"] for a in assocs}
        assert ids[1] not in assoc_ids
        assert ids[2] not in assoc_ids

    def test_empty_memory_ids(self, tmp_db):
        assert get_associations(tmp_db, memory_ids=[]) == []

    def test_max_results_cap(self, seeded_db):
        tmp_db, ids = seeded_db
        assocs = get_associations(tmp_db, memory_ids=[ids[0]], max_results=1)
        assert len(assocs) <= 1


class TestHubExclusion:
    def test_hub_excluded_after_5_sessions(self, tmp_db):
        for i in range(6):
            mems = ["hub_mem", f"normal_{i}", f"other_{i}"]
            record_memory_load(tmp_db, session_id=f"s{i}", memory_ids=mems)
            compute_co_occurrences(tmp_db, session_id=f"s{i}", session_score=12, max_score=15)
        record_memory_load(tmp_db, session_id="s_new", memory_ids=["hub_mem", "new_a", "new_b"])
        pairs = compute_co_occurrences(tmp_db, session_id="s_new", session_score=12, max_score=15)
        assert pairs == 1


class TestCoOccurrenceStats:
    def test_returns_stats(self, seeded_db):
        tmp_db, ids = seeded_db
        stats = get_co_occurrence_stats(tmp_db)
        assert stats["total_pairs"] == 3
        assert stats["total_sessions_tracked"] == 3
        assert stats["avg_strength"] > 0

    def test_empty_db(self, tmp_db):
        get_db(tmp_db)
        stats = get_co_occurrence_stats(tmp_db)
        assert stats["total_pairs"] == 0
        assert stats["total_sessions_tracked"] == 0
