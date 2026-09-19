"""`anchor("help", "memory")` must document the id retrieval mode.

The `memory_id` lookup landed in b9bf8b9, but help only ever showed `memory_id` as
an identifier for lifecycle operations — apply, disposition, evaluate, promotion.
An agent handed an id by a disposition block could not tell from help that the id
was resolvable at all, which is the gap that sent one to the SQLite store.
"""
from odibi_anchor._dispatcher._dispatch_table import build_help_text

HELP = build_help_text("memory", None)


def test_help_shows_the_retrieval_call():
    assert 'anchor("memory", memory_id="...")' in HELP


def test_help_explains_why_a_query_cannot_substitute():
    """The reason the parameter has to exist at all."""
    assert "not part of the entry content" in HELP


def test_help_says_the_lookup_is_not_scoped():
    """Documenting the scoping is the point: a scoped lookup would be a dead end."""
    assert "not scoped by project or status" in HELP


def test_help_states_the_result_shape():
    for key in ('"kind": "memory_entry"', '"found"', '"entries"', '"count"'):
        assert key in HELP, f"{key} missing from memory help"


def test_help_states_the_not_found_behaviour():
    assert "found=False" in HELP
    assert "never an unrelated match" in HELP


def test_lifecycle_uses_of_memory_id_are_still_documented():
    """Negative control: the retrieval entry must not displace the existing ones."""
    for command in ("`apply`", "`disposition`", "`evaluate`"):
        assert command in HELP


def test_documented_shape_matches_the_implementation(tmp_path):
    """Guard against the help drifting from what the lookup actually returns."""
    from odibi_anchor.codebase._memory_db import insert_memory
    from odibi_anchor.codebase.memory_context import memory_context

    db = str(tmp_path / "memory.db")
    created = insert_memory(
        db, project="alpha", type="discovery", content="An entry to resolve.",
        related_files=[], tags=[], source="test", confidence=0.4, status="candidate",
    )
    result = memory_context(tmp_path, memory_id=created["id"], db_path=db, output_format="dict")
    assert result["kind"] == "memory_entry"
    assert set(("kind", "found", "entries", "count")).issubset(result)

    missing = memory_context(
        tmp_path, memory_id="00000000-0000-0000-0000-000000000000",
        db_path=db, output_format="dict",
    )
    assert missing["found"] is False
    assert missing["entries"] == []
    assert missing["reason"]
