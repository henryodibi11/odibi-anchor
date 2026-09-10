"""Regression tests: exposure and successful gates never confirm memory."""

from odibi_anchor._dispatcher._memory import (
    _auto_confirm_eligible,
    _auto_confirm_surfaced,
)
from odibi_anchor.codebase._memory_db import get_db, insert_memory


def test_surfaced_compatibility_hook_never_promotes(tmp_path):
    db = str(tmp_path / "memory.db")
    entry = insert_memory(
        db, project="test", type="gotcha", content="Long actionable guidance", status="candidate"
    )
    conn = get_db(db)
    conn.execute("UPDATE memories SET use_count = 999 WHERE id = ?", (entry["id"],))
    conn.commit()

    assert _auto_confirm_surfaced({"entries": [{**entry, "use_count": 999}]}, db) is None
    row = conn.execute(
        "SELECT status, confirmation_count FROM memories WHERE id = ?", (entry["id"],)
    ).fetchone()
    assert dict(row) == {"status": "candidate", "confirmation_count": 0}


def test_gate_bulk_promotion_compatibility_hook_is_noop(tmp_path):
    db = str(tmp_path / "memory.db")
    entry = insert_memory(
        db, project="test", type="pattern", content="Long verified-looking guidance", status="candidate"
    )
    conn = get_db(db)
    conn.execute("UPDATE memories SET use_count = 999 WHERE id = ?", (entry["id"],))
    conn.commit()

    assert _auto_confirm_eligible(db) == 0
    assert conn.execute("SELECT status FROM memories WHERE id = ?", (entry["id"],)).fetchone()[0] == "candidate"
