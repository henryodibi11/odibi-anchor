"""Deterministic, non-destructive curation of obsolete operational memory."""
from __future__ import annotations

import sqlite3
from pathlib import Path


def _classify_unsafe_guidance(content: str) -> list[str]:
    """Classify narrowly actionable bypass advice, not historical references."""
    text = content.lower()
    reasons = set()
    if "_session_state" in text and any(phrase in text for phrase in (
        "bypass", "workaround: set", "set _session_state", "fix: set _session_state",
        "_session_touched() directly",
    )):
        reasons.add("direct-state-bypass")
    if "sandbox" in text and "create empty" in text and any(
        phrase in text for phrase in ("satisfy", "unblock", "gate needs")
    ):
        reasons.add("sandbox-bypass")
    if any(phrase in text for phrase in ("five-file", "5-file", "file limit")) and any(
        phrase in text for phrase in ("workaround", "bypass")
    ):
        reasons.add("file-limit-bypass")
    if any(phrase in text for phrase in ("modify agent_init.py", "edit agent_init.py")):
        reasons.add("removed-agent-init")
    return sorted(reasons)


def memory_hygiene_context(
    *, db_path: str | None = None, dry_run: bool = True,
    apply_ids: list[str] | None = None, **_kwargs,
) -> dict:
    """Report unsafe present-tense guidance and optionally quarantine exact IDs.

    Applying requires both ``dry_run=False`` and an explicit ``apply_ids`` list.
    Rows and source text are never deleted. Historical architecture references are
    protected by requiring an actionable phrase rather than a bare removed filename.
    """
    from odibi_anchor.codebase._memory_db import _DEFAULT_DB_PATH

    target = db_path or _DEFAULT_DB_PATH
    requested = list(dict.fromkeys(apply_ids or []))
    if not dry_run and not requested:
        raise ValueError("apply_ids is required; run dry_run first and review exact IDs")
    # A dry run must not trigger additive migrations or otherwise mutate the DB.
    if dry_run and not Path(target).is_file():
        raise FileNotFoundError(f"Memory database does not exist: {target}")
    uri = Path(target).resolve().as_uri() + "?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True) if dry_run else sqlite3.connect(target)
    if dry_run:
        conn.execute("PRAGMA query_only=ON")
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, content, status FROM memories ORDER BY id"
    ).fetchall()
    conn.close()
    matches = []
    for row in rows:
        reasons = _classify_unsafe_guidance(row["content"])
        if reasons and row["status"] not in {"quarantined", "retired", "superseded"}:
            matches.append({"id": row["id"], "reasons": reasons,
                            "content": row["content"][:160]})

    reported_ids = [item["id"] for item in matches]
    invalid = sorted(set(requested) - set(reported_ids))
    if invalid:
        raise ValueError(f"apply_ids were not in this deterministic report: {invalid}")

    applied = []
    if not dry_run:
        conn = sqlite3.connect(target)
        for entry_id in requested:
            conn.execute(
                "UPDATE memories SET status = 'quarantined' WHERE id = ?", (entry_id,)
            )
            applied.append(entry_id)
        conn.commit()
        conn.close()

    return {
        "kind": "memory_hygiene",
        "subject": target,
        "summary": f"{'DRY RUN: ' if dry_run else ''}{len(matches)} unsafe guidance candidates; {len(applied)} quarantined",
        "metrics": {"dry_run": dry_run, "reported": len(matches),
                    "quarantined": len(applied), "deleted": 0},
        "findings": [f"{item['id']}: {', '.join(item['reasons'])}" for item in matches],
        "risks": (["No changes applied; pass dry_run=False with reviewed apply_ids."]
                  if dry_run else []),
        "samples": {"candidates": matches[:20], "reported_ids": reported_ids,
                    "applied_ids": applied},
        "suggested_next_actions": (["Review reported_ids, then apply only approved IDs."]
                                   if dry_run else ["Inspect quarantined entries with an explicit status query."]),
    }
