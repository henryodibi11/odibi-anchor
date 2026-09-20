"""An agent handed a memory id must be able to resolve it through Anchor.

Memory ids are given to callers in two places — the disposition block that
precedes `learning assess`, and the projection decisions `assess` returns — but
`memory_context` had no id parameter at all. A free-text query for the id matches
nothing, because the id is not part of the entry content, so the only way to see
what an id referred to was to open the SQLite store directly.
"""
from typing import Any

import pytest

from odibi_anchor.codebase._memory_db import insert_memory
from odibi_anchor.codebase.memory_context import memory_context

# `get_memory_entry` is imported inside the two tests that exercise it directly, so
# the behavioural tests below fail on their assertions rather than at collection
# when run against a build that does not have it yet.


def _memory_context_dict(*args, **kwargs) -> dict[str, Any]:
    result = memory_context(*args, **kwargs, output_format="dict")
    assert isinstance(result, dict)
    return result


@pytest.fixture
def store(tmp_path):
    """A store holding one project-scoped entry and one cross-project entry."""
    db = str(tmp_path / "memory.db")
    local = insert_memory(
        db, project="alpha", type="discovery",
        content="Alpha only: the launcher reconciles guidance before binding.",
        related_files=[], tags=[], source="test", confidence=0.4, status="candidate",
    )
    shared = insert_memory(
        db, project="all", type="convention",
        content="Cross project: never edit the portfolio TOML by hand.",
        related_files=[], tags=[], source="test", confidence=0.4, status="candidate",
    )
    return db, local["id"], shared["id"]


def test_exact_id_returns_that_entry(store, tmp_path):
    db, local_id, _ = store
    result = _memory_context_dict(tmp_path, memory_id=local_id, db_path=db, project="alpha")
    assert result["found"] is True
    assert result["count"] == 1
    assert result["entries"][0]["id"] == local_id
    assert "Alpha only" in result["entries"][0]["content"]


def test_lookup_reports_the_entry_scope_for_the_owning_project(store, tmp_path):
    db, local_id, _ = store
    entry = _memory_context_dict(
        tmp_path, memory_id=local_id, db_path=db, project="alpha",
    )["entries"][0]
    assert entry["project"] == "alpha"
    assert entry["status"] == "candidate"


def test_lookup_does_not_cross_project_scope(store, tmp_path):
    db, local_id, _ = store
    result = _memory_context_dict(
        tmp_path, memory_id=local_id, db_path=db, project="a-different-project",
    )
    assert result["found"] is False
    assert result["entries"] == []


def test_lookup_allows_shared_memory_from_any_project(store, tmp_path):
    db, _, shared_id = store
    result = _memory_context_dict(tmp_path, memory_id=shared_id, db_path=db, project="beta")
    assert result["found"] is True
    assert result["entries"][0]["id"] == shared_id


def test_lookup_hides_non_retrievable_lifecycle_states(store, tmp_path):
    db, local_id, _ = store
    from odibi_anchor.codebase._memory_db import get_db

    connection = get_db(db)
    connection.execute("UPDATE memories SET status='rejected' WHERE id=?", (local_id,))
    connection.commit()
    result = _memory_context_dict(tmp_path, memory_id=local_id, db_path=db, project="alpha")
    assert result["found"] is False


def test_unknown_id_reports_not_found_rather_than_unrelated_entries(store, tmp_path):
    db, _, _ = store
    result = _memory_context_dict(
        tmp_path, memory_id="00000000-0000-0000-0000-000000000000",
        db_path=db,
    )
    assert result["found"] is False
    assert result["entries"] == []
    assert "no retrievable memory entry has id" in result["reason"]


def test_free_text_query_for_the_id_still_finds_nothing(store, tmp_path):
    """Why the parameter is needed: the id is not in the content, so search cannot work."""
    db, local_id, _ = store
    result = _memory_context_dict(tmp_path, query=local_id, db_path=db)
    returned = [entry["id"] for entry in result.get("entries", [])]
    assert local_id not in returned


def test_ordinary_queries_are_unchanged(store, tmp_path):
    """Negative control: adding the parameter must not alter normal retrieval."""
    db, _, shared_id = store
    result = _memory_context_dict(
        tmp_path, query="portfolio TOML", db_path=db, project="alpha",
    )
    assert shared_id in [entry["id"] for entry in result.get("entries", [])]


def test_markdown_rendering_shows_identity_and_content(store, tmp_path):
    db, local_id, _ = store
    text = memory_context(
        tmp_path, memory_id=local_id, db_path=db, project="alpha", output_format="markdown",
    )
    assert local_id in text
    assert "alpha" in text
    assert "Alpha only" in text


def test_markdown_rendering_of_a_miss_says_not_found(store, tmp_path):
    db, _, _ = store
    text = memory_context(
        tmp_path, memory_id="11111111-1111-1111-1111-111111111111",
        db_path=db, output_format="markdown",
    )
    assert "Not found" in text


# ── the storage helper itself ────────────────────────────────────────────────


def test_helper_returns_none_for_an_absent_id(store):
    from odibi_anchor.codebase._memory_db import get_memory_entry

    db, _, _ = store
    assert get_memory_entry(db, entry_id="22222222-2222-2222-2222-222222222222") is None


@pytest.mark.parametrize("bad", ["", "   ", None, 7])
def test_helper_rejects_an_empty_or_non_string_id(store, bad):
    from odibi_anchor.codebase._memory_db import get_memory_entry

    db, _, _ = store
    with pytest.raises(ValueError):
        get_memory_entry(db, entry_id=bad)
