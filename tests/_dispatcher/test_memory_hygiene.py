"""Non-destructive, reviewed-ID memory hygiene coverage."""

import pytest

from odibi_anchor.codebase._memory_db import get_db, insert_memory
from odibi_anchor.codebase.memory_hygiene_context import (
    _classify_unsafe_guidance,
    memory_hygiene_context,
)


def _seed(db):
    unsafe = insert_memory(
        db, project="p", type="gotcha", content="Modify agent_init.py to bypass the current workflow"
    )["id"]
    historical = insert_memory(
        db, project="p", type="decision", content="Historical architecture used agent_init.py"
    )["id"]
    return unsafe, historical


def test_dry_run_is_deterministic_and_non_mutating(tmp_path):
    db = str(tmp_path / "memory.db")
    unsafe, historical = _seed(db)
    first = memory_hygiene_context(db_path=db)
    second = memory_hygiene_context(db_path=db)
    assert first["samples"]["reported_ids"] == second["samples"]["reported_ids"] == [unsafe]
    rows = dict(get_db(db).execute("SELECT id, status FROM memories"))
    assert rows[unsafe] == rows[historical] == "candidate"
    assert first["metrics"]["deleted"] == 0


def test_apply_requires_exact_reviewed_ids_and_quarantines_without_delete(tmp_path):
    db = str(tmp_path / "memory.db")
    unsafe, historical = _seed(db)
    with pytest.raises(ValueError, match="apply_ids is required"):
        memory_hygiene_context(db_path=db, dry_run=False)
    with pytest.raises(ValueError, match="not in this deterministic report"):
        memory_hygiene_context(db_path=db, dry_run=False, apply_ids=[historical])

    result = memory_hygiene_context(db_path=db, dry_run=False, apply_ids=[unsafe, unsafe])
    rows = {row["id"]: dict(row) for row in get_db(db).execute("SELECT * FROM memories")}
    assert result["samples"]["applied_ids"] == [unsafe]
    assert rows[unsafe]["status"] == "quarantined"
    assert rows[unsafe]["content"].startswith("Modify agent_init.py")
    assert rows[historical]["status"] == "candidate"
    assert len(rows) == 2


def test_purge_delete_options_are_unavailable(tmp_path):
    db = str(tmp_path / "memory.db")
    _seed(db)
    before = get_db(db).execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    result = memory_hygiene_context(db_path=db, purge_projects=["p"])
    assert result["metrics"]["deleted"] == 0
    assert get_db(db).execute("SELECT COUNT(*) FROM memories").fetchone()[0] == before


@pytest.mark.parametrize("content", [
    "Workaround: set _SESSION_STATE.spec_persisted=True directly",
    "Sandbox gate needs a directory; create empty sandbox to unblock it",
    "The 5-file limit workaround is to bypass touched",
])
def test_actionable_bypass_guidance_is_classified(content):
    assert _classify_unsafe_guidance(content)


@pytest.mark.parametrize("content", [
    "Architecture fix moved session state out of agent_init.py",
    "The former sandbox manifest recorded source hashes",
    "A historical 5-file deadlock motivated the current checkpoint design",
    "Two module instances held a different set of session state objects",
])
def test_historical_architecture_is_not_classified(content):
    assert _classify_unsafe_guidance(content) == []
