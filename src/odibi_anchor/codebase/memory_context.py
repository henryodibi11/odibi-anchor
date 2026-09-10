"""odibi_anchor.codebase.memory_context — Queryable project memory.

SQLite-backed memory system with FTS5 search, cross-project sharing,
deduplication, and confidence-based lifecycle management.

Usage:
    from odibi_anchor.codebase import memory_context

    # Query memories relevant to current work
    ctx = memory_context(
        root="/path/to/project",
        files_involved=["src/odibi_anchor/_utils/contract.py"],
        task_type="modify_code",
    )

    # Record a new memory
    from odibi_anchor.codebase.memory_context import append_memory
    append_memory(
        root="/path/to/project",
        entry_type="gotcha",
        content="Editing contract.py requires updating test_output_schema.py",
        related_files=["src/**/_utils/contract.py", "tests/test_output_schema.py"],
        tags=["contract", "testing"],
    )

Dependencies: stdlib only (sqlite3, pathlib, re, fnmatch).
"""

from __future__ import annotations

import logging
import re
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

from odibi_anchor._utils.contract import validate_output_format, build_base_context
from odibi_anchor._utils.render_utils import (
    render_header_lines, render_metrics_lines, render_bullet_section,
)
from odibi_anchor.codebase._memory_db import (
    resolve_project,
    insert_memory as _db_insert,
    query_memories as _db_query,
    confirm_memory_entry as _db_confirm,
    reject_memory_entry as _db_reject,
    archive_stale as _db_archive,
    get_all_entries as _db_get_all,
    entry_count as _db_entry_count,
    record_surfaced as _db_record_surfaced,
    get_annotations_for_file as _db_get_file_annotations,
    get_annotations_for_table as _db_get_table_annotations,
    get_relevant_trails as _db_get_trails,
    VALID_TYPES,
    VALID_STATUSES,
    _DEFAULT_DB_PATH,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants (kept for backward compatibility)
# ---------------------------------------------------------------------------

_ARCHIVE_FILE = ".agent_memory_archive.jsonl"
_VALID_ENTRY_TYPES = VALID_TYPES
_VALID_STATUSES_SET = VALID_STATUSES




def _get_current_session_id() -> str | None:
    """Return stable identity only after the current session has been named."""
    try:
        from odibi_anchor._utils._session_state import get_state
        state = get_state()
        return state.get("session_id") if state.get("session_name") else None
    except Exception:
        return None

# ---------------------------------------------------------------------------
# Public API: Query
# ---------------------------------------------------------------------------


def memory_context(
    root: str | Path,
    *,
    files_involved: list[str] | None = None,
    tags: list[str] | None = None,
    error_text: str | None = None,
    task_type: str | None = None,
    entry_type: str | None = None,
    query: str | None = None,
    limit: int = 10,
    offset: int = 0,
    include_archived: bool = False,
    include_retired: bool = False,
    status: str | list[str] | None = None,
    surfaced: bool = False,
    subject: str | None = None,
    output_format: str = "dict",
    db_path: str | None = None,
    project: str | None = None,
) -> dict[str, Any] | str:
    """Query project memory for entries relevant to the current task.

    Retrieves memory entries filtered and scored by relevance. Entries are
    matched against file paths (glob patterns), tags, error text (substring),
    task type, and free-text query. Results are ranked by relevance score
    with confidence and explicit-evidence boosts.

    Args:
        root: Project root directory.
        files_involved: List of file paths currently being worked on.
            Matched against entry related_files using glob patterns.
        tags: List of tags to filter by. Entries matching any tag score higher.
        error_text: Error text to match against. Particularly useful for
            entries of type "gotcha" and "failure_pattern".
        task_type: Current task type (e.g., "modify_code", "debug",
            "data_pipeline", "refactor"). Matched against entry tags.
        entry_type: Filter to a specific entry type ("gotcha", "decision",
            "pattern", "convention", "failure_pattern").
        query: Free-text search query. Matched against entry content.
        limit: Maximum number of entries to return (default 10).
        offset: Number of ranked matching entries to skip (default 0).
        include_archived: If True, also search stale entries.
        include_retired: If True, include entries with status "retired".
        surfaced: Record explicit exposure telemetry for the returned entries.
            Defaults to False so retrieval remains a read-only operation.
        subject: Human label. Defaults to directory name.
        output_format: "dict" or "markdown".
        db_path: Path to SQLite DB (defaults to central DB).

    Returns:
        Structured context dict (or markdown string).
    """
    validate_output_format(output_format)
    if type(limit) is not int:
        raise TypeError("limit must be a positive int")
    if limit < 1:
        raise ValueError("limit must be a positive int")
    if type(offset) is not int:
        raise TypeError("offset must be a non-negative int")
    if offset < 0:
        raise ValueError("offset must be a non-negative int")

    root = Path(root).resolve()
    subject = subject or root.name
    project = project or resolve_project(root)
    db_path = db_path or _DEFAULT_DB_PATH

    # Build status filter. Explicit inspection is validated rather than using
    # ``None`` (which means the normal retrievable statuses in query_memories).
    status_filter: list[str] | None = None
    if status is not None:
        status_filter = [status] if isinstance(status, str) else list(status)
        invalid = set(status_filter) - set(VALID_STATUSES)
        if invalid:
            raise ValueError(f"Invalid memory status: {sorted(invalid)}")
    elif include_retired:
        status_filter = list(VALID_STATUSES)
    elif not include_archived:
        status_filter = ["candidate", "active", "confirmed"]
    elif include_archived and not include_retired:
        status_filter = ["candidate", "active", "confirmed", "stale"]
    # else: None means no filter (returns everything)

    # Build the search query for FTS (only for explicit query parameter)
    fts_query = query if query else ""

    # Query via SQLite backend
    raw_results = _db_query(
        db_path,
        project=project,
        query=fts_query if fts_query else None,
        type=entry_type,
        status=status_filter,
        tags=tags,
        related_file=files_involved[0] if files_involved and len(files_involved) == 1 else None,
        limit=None,  # Rank the complete eligible scoped corpus before bounding output.
    )

    # Apply Python-side scoring for complex relevance (files, error text, task_type)
    scored = _score_entries(
        raw_results,
        files_involved=files_involved or [],
        tags=tags or [],
        error_text=error_text or "",
        task_type=task_type or "",
        query=query or "",
        # An explicit status is an inspection request and must not be undone by
        # the normal terminal-status scoring guard.
        include_retired=include_retired or status is not None,
    )

    # Page the final ranked result set, equivalent to [offset:offset + limit].
    results = scored[offset:offset + limit]
    total_matches = len(scored)
    next_offset = offset + len(results)
    has_more = next_offset < total_matches

    # Legacy co-occurrence storage remains readable for compatibility, but it
    # is not a current scoped evidence source and must not affect retrieval.
    associated_entries: list[dict] = []

    # --- Marginalia: surface location-bound annotations ---
    file_annotations: list[dict] = []
    table_annotations: list[dict] = []
    if results:
        # Check if any results reference files or tables
        seen_files: set[str] = set()
        seen_tables: set[str] = set()
        for r in results:
            for f in r.get("related_files", []):
                seen_files.add(f)
            # If subject looks like a table name (contains dots), check for annotations
        if subject and "." in subject:
            seen_tables.add(subject)

        for fp in list(seen_files)[:5]:
            file_annotations.extend(_db_get_file_annotations(db_path, file_path=fp))
        for tn in list(seen_tables)[:3]:
            table_annotations.extend(_db_get_table_annotations(db_path, table_name=tn))

    # --- Named Trails: surface relevant playbooks ---
    relevant_trails: list[dict] = []
    if subject:
        relevant_trails = _db_get_trails(db_path, task_goal=subject, keyword=subject.split()[0] if subject else "", max_results=2)

    # Build output
    counts = _db_entry_count(db_path, project=project)
    metrics = {
        "total_entries": counts.get("total", 0),
        "matched_entries": len(scored),
        "returned_entries": len(results),
        "total_matches": total_matches,
        "returned_count": len(results),
        "offset": offset,
        "next_offset": next_offset if has_more else None,
        "has_more": has_more,
        "has_results": len(results) > 0,
        "project": project,
        "associated_count": len(associated_entries),
        "file_annotations": len(file_annotations),
        "table_annotations": len(table_annotations),
        "relevant_trails": len(relevant_trails),
    }

    findings = []
    if results:
        type_counts: dict[str, int] = {}
        for r in results:
            t = r.get("type", "unknown")
            type_counts[t] = type_counts.get(t, 0) + 1
        breakdown = ", ".join(f"{v} {k}(s)" for k, v in type_counts.items())
        findings.append(f"Found {len(results)} relevant entries: {breakdown}.")
    else:
        findings.append("No relevant memory entries found for this context.")

    risks = []
    unvalidated = [r for r in results if not r.get("last_confirmed")]
    if unvalidated:
        risks.append(
            f"{len(unvalidated)} returned entries lack historical independent validation — "
            "treat candidates as advisory and verify against current evidence."
        )

    suggested_actions = [
        # ── Graph wiring (audit fix) ──
        "SHOULD: Keep candidates advisory and verify them against current evidence; retrieval, "
        "application, evaluation, and counters do not promote.",
        'SHOULD: For task-selected memories, record disposition and use anchor("memory", "apply|evaluate", ...) when they materially affect work.',
        'SHOULD: Run anchor("reject", entry_id) for entries that were wrong.',
        'COULD: Inspect rejected/stale/quarantined entries with an explicit status query.',
    ]
    if not results and (files_involved or error_text):
        suggested_actions.append(
            "COULD: At learning closure, capture only a supported reusable observation; "
            "novelty alone does not require a memory save."
        )

    summary = (
        f"{len(results)} relevant memory entries for {subject}"
        if results
        else f"No relevant memories found for current context"
    )

    # ── Cap auxiliary lists with size hints (AXI content truncation) ──
    # Counts in metrics (associated_count/file_annotations/...) stay accurate above.
    from odibi_anchor._utils.contract import cap_with_hint
    associated_entries, _a_hint = cap_with_hint(associated_entries, 15, unit="associated entries")
    file_annotations, _f_hint = cap_with_hint(file_annotations, 15, unit="file annotations")
    table_annotations, _t_hint = cap_with_hint(table_annotations, 15, unit="table annotations")
    for _h in (_a_hint, _f_hint, _t_hint):
        if _h:
            findings.append(_h)

    ctx = build_base_context(
        kind="memory_context",
        subject=subject,
        summary=summary,
        metrics=metrics,
        findings=findings,
        risks=risks,
        samples={},
        suggested_next_actions=suggested_actions,
        entries=results,
        associated_entries=associated_entries,
        file_annotations=file_annotations,
        table_annotations=table_annotations,
        relevant_trails=relevant_trails,
    )

    # Exposure telemetry is opt-in. Routine retrieval, including orientation,
    # must not mutate the durable memory store merely because an entry was read.
    if surfaced:
        visible_ids = [r.get("id") or r.get("memory_id", "")
                       for r in results + associated_entries
                       if r.get("id") or r.get("memory_id")]
        _db_record_surfaced(db_path, entry_ids=visible_ids,
                            session_id=_get_current_session_id() or "")

    if output_format == "markdown":
        return render_memory_report(ctx)
    return ctx


# ---------------------------------------------------------------------------
# Public API: Write
# ---------------------------------------------------------------------------


def append_memory(
    root: str | Path,
    *,
    entry_type: str,
    content: str,
    related_files: list[str] | None = None,
    tags: list[str] | None = None,
    source: str | None = None,
    confidence: float = 0.5,
    evidence: dict[str, Any] | None = None,
    project: str | None = None,
    db_path: str | None = None,
) -> dict[str, Any]:
    """Append a new memory entry to the SQLite store.

    Args:
        root: Project root directory.
        entry_type: One of "gotcha", "decision", "pattern", "convention",
            "failure_pattern", "discovery", "tool_call", "preference".
        content: The memory content — concise, factual, actionable.
        related_files: Glob patterns for files this entry relates to.
        tags: Tags for retrieval filtering.
        source: Where this memory came from.
        confidence: Initial confidence score (0.0-1.0).
        evidence: Optional structured dict with supporting data.
        project: Override project name. Defaults to auto-detected from root.
            Use "all" for cross-cutting knowledge.
        db_path: Path to SQLite DB (defaults to central DB).

    Returns:
        The created entry dict (with generated id and timestamps).

    Raises:
        ValueError: If entry_type is not valid.
    """
    root = Path(root).resolve()
    resolved_project = project or resolve_project(root)
    db_path = db_path or _DEFAULT_DB_PATH

    if related_files is None:
        try:
            from odibi_anchor._utils._session_state import get_state
            state = get_state()
            related_files = list(dict.fromkeys(
                state.get("files_changed", []) + state.get("files_created", [])
            ))
        except Exception:
            related_files = []
    related_files = list(dict.fromkeys(related_files or []))
    tags = list(dict.fromkeys(tags or []))
    for path in related_files:
        file_tag = f"file:{path}"
        if file_tag not in tags:
            tags.append(file_tag)

    result = _db_insert(
        db_path,
        project=resolved_project,
        type=entry_type,
        content=content,
        related_files=related_files,
        tags=tags,
        source=source or "manual",
        confidence=confidence,
        status="candidate",
        evidence=evidence,
    )

    return result


def confirm_memory(
    root: str | Path,
    entry_id: str,
    *,
    db_path: str | None = None,
    **kwargs: Any,
) -> dict[str, Any] | None:
    """Return the fail-closed runtime promotion status.

    This compatibility API cannot authenticate human authority and the current
    recurrence rows do not carry terminal/evidence-authoritative lineage.  Any
    legacy or free-form authority payload is retained only as a blocked signal.

    Args:
        root: Project root directory (kept for backward compat, not used).
        entry_id: The memory entry ID to confirm.
        db_path: Path to SQLite DB.

    Returns:
        The updated entry dict, or None if not found.
    """
    db_path = db_path or _DEFAULT_DB_PATH
    try:
        return _db_confirm(db_path, entry_id=entry_id, **kwargs)
    except ValueError:
        return None


def reject_memory(
    root: str | Path,
    entry_id: str,
    *,
    db_path: str | None = None,
    **kwargs: Any,
) -> dict[str, Any] | None:
    """Mark a memory entry as a false positive.

    Excludes the entry from normal retrieval immediately and records rejection evidence.

    Args:
        root: Project root directory (kept for backward compat, not used).
        entry_id: The memory entry ID to reject.
        db_path: Path to SQLite DB.

    Returns:
        The updated entry dict, or None if not found.
    """
    db_path = db_path or _DEFAULT_DB_PATH
    try:
        return _db_reject(db_path, entry_id=entry_id)
    except ValueError:
        return None


def archive_stale_entries(
    root: str | Path,
    *,
    max_unused_days: int = 90,
    db_path: str | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Mark low-evidence old entries stale using the legacy compatibility action.

    Marks entries as 'stale' if not used within max_unused_days
    and use_count is below threshold. Confirmed entries are protected.

    Args:
        root: Project root directory (kept for backward compat).
        max_unused_days: Days since last_used before archiving.
        db_path: Path to SQLite DB.

    Returns:
        Dict with "archived_count" and summary.
    """
    db_path = db_path or _DEFAULT_DB_PATH
    result = _db_archive(db_path, max_unused_days=max_unused_days)
    # Add backward-compat keys
    counts = _db_entry_count(db_path)
    result["remaining_count"] = counts.get("total", 0) - result.get("archived_count", 0)
    return result


def export_markdown(
    root: str | Path,
    *,
    db_path: str | None = None,
    project: str | None = None,
    **kwargs: Any,
) -> str:
    """Export all active memory entries as readable markdown.

    Args:
        root: Project root directory.
        db_path: Path to SQLite DB.

    Returns:
        Markdown string.
    """
    root = Path(root).resolve()
    project = project or resolve_project(root)
    db_path = db_path or _DEFAULT_DB_PATH

    entries = _db_get_all(
        db_path,
        project=project,
        status=["candidate", "active", "confirmed"],
    )

    lines = [f"# Agent Memory — {project}", ""]

    # Group by type
    by_type: dict[str, list[dict]] = {}
    for e in entries:
        t = e.get("type", "unknown")
        by_type.setdefault(t, []).append(e)

    type_headers = {
        "gotcha": "Gotchas",
        "decision": "Decisions",
        "pattern": "Patterns",
        "convention": "Conventions",
        "failure_pattern": "Failure Patterns",
        "discovery": "Discoveries",
        "preference": "Preferences",
    }

    for entry_type_key, header in type_headers.items():
        group = by_type.get(entry_type_key, [])
        if not group:
            continue
        lines.append(f"## {header}")
        lines.append("")
        for e in group:
            tags_str = f" [{', '.join(e.get('tags', []))}]" if e.get("tags") else ""
            conf = f" (conf={e.get('confidence', 0):.1f})" if e.get("confidence", 0) >= 0.7 else ""
            lines.append(f"- **{e['id'][:8]}**{tags_str}{conf}: {e['content']}")
            if e.get("related_files"):
                files_str = ", ".join(f"`{f}`" for f in e["related_files"][:5])
                lines.append(f"  - Files: {files_str}")
        lines.append("")

    return "\n".join(lines)


def import_from_markdown(
    root: str | Path,
    markdown_path: str | Path | None = None,
    *,
    db_path: str | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Import entries from a markdown file into the SQLite store.

    Reads the markdown file, extracts entries by section, and inserts them
    into the database with deduplication.

    Args:
        root: Project root directory.
        markdown_path: Path to the markdown file to import.
            Defaults to root/.agent_memory.md.
        db_path: Path to SQLite DB.

    Returns:
        Dict with "imported_count" and "skipped_count".
    """
    root = Path(root).resolve()
    project = resolve_project(root)
    md_path = Path(markdown_path) if markdown_path else root / ".agent_memory.md"
    db_path = db_path or _DEFAULT_DB_PATH

    if not md_path.exists():
        return {"imported_count": 0, "skipped_count": 0}

    content = md_path.read_text(encoding="utf-8")
    lines = content.splitlines()

    # Map section headers to entry types
    section_map = {
        "gotchas": "gotcha",
        "decisions": "decision",
        "patterns": "pattern",
        "conventions": "convention",
        "failure patterns": "failure_pattern",
        "discoveries": "discovery",
        "preferences": "preference",
    }

    imported = 0
    skipped = 0
    current_type: str | None = None

    for line in lines:
        stripped = line.strip()

        # Check for section headers
        if stripped.startswith("## "):
            header_text = stripped[3:].strip().lower()
            current_type = section_map.get(header_text)
            continue

        # Skip non-entry lines
        if not current_type:
            continue
        if not stripped.startswith("- "):
            continue

        # Parse entry
        entry_text = stripped[2:].strip()
        # Remove bold markers: **key**: value → key: value
        entry_text = re.sub(r"\*\*([^*]+)\*\*:\s*", r"\1: ", entry_text)
        # Remove leading ID-like patterns (m0001:, abc12345:)
        entry_text = re.sub(r"^[a-z0-9-]{4,10}:\s*", "", entry_text)
        # Remove tag brackets [tag1, tag2]
        entry_text = re.sub(r"\s*\[[^\]]+\]\s*", " ", entry_text).strip()

        if not entry_text:
            continue

        result = _db_insert(
            db_path,
            project=project,
            type=current_type,
            content=entry_text,
            tags=_extract_tags_from_content(entry_text),
            source="import_from_markdown",
            confidence=0.5,
            status="candidate",
        )

        if result["action"] == "inserted":
            imported += 1
        else:
            skipped += 1

    return {"imported_count": imported, "skipped_count": skipped}


# ---------------------------------------------------------------------------
# Scoring / Retrieval
# ---------------------------------------------------------------------------

# Regex for tokenizing text the same way FTS5 unicode61 does:
# splits on any non-alphanumeric/underscore boundary.
_TOKEN_RE = re.compile(r"[a-z0-9_]+")


def _tokenize_text(text: str) -> set[str]:
    """Tokenize text matching FTS5 unicode61 behavior.

    Splits on dots, backticks, parens, and all non-word characters.
    Returns lowercase token set. E.g.:
        "`importlib.reload()`" → {"importlib", "reload"}
        "queue-automation"     → {"queue", "automation"}
    """
    return set(_TOKEN_RE.findall(text.lower()))


def _score_entries(
    entries: list[dict[str, Any]],
    *,
    files_involved: list[str],
    tags: list[str],
    error_text: str,
    task_type: str,
    query: str,
    include_retired: bool = False,
) -> list[dict[str, Any]]:
    """Score and rank entries by relevance.

    When no scoring signals are provided (no query, tags, files, error, or
    task_type), all entries receive a baseline score and are returned sorted
    by recency. Otherwise only entries with score > 0 are returned.

    Entries with status "retired" are excluded unless include_retired=True.
    """
    scored: list[tuple[float, dict[str, Any]]] = []

    tags_lower = {t.lower() for t in tags}
    error_lower = error_text.lower()
    query_lower = query.lower()
    task_lower = task_type.lower()

    # Detect "browse all" mode: no scoring signals provided
    has_signals = bool(files_involved or tags_lower or error_lower or query_lower or task_lower)

    for entry in entries:
        # Skip retired entries unless explicitly requested
        status = entry.get("status", "active")
        if status == "retired" and not include_retired:
            continue

        score = 0.0
        content_lower = entry.get("content", "").lower()
        entry_tags = {t.lower() for t in entry.get("tags", [])}
        entry_files = entry.get("related_files", [])

        # Baseline: when no signals, include all entries with minimal score
        if not has_signals:
            score = 0.1

        # File match: each involved file contributes at most once.
        if files_involved and entry_files:
            for involved_file in files_involved:
                matched = False
                for pattern in entry_files:
                    if fnmatch(involved_file, pattern) or fnmatch(involved_file, f"**/{pattern}"):
                        score += 3.0
                        matched = True
                        break
                if not matched:
                    for pattern in entry_files:
                        if "*" not in pattern and pattern in involved_file:
                            score += 2.0
                            break

        # Tag match
        if tags_lower and entry_tags:
            overlap = tags_lower & entry_tags
            score += len(overlap) * 2.0

        # Task type match against tags
        if task_lower and task_lower in entry_tags:
            score += 1.5

        # Error text match (for gotchas and failure patterns)
        if error_lower and entry.get("type") in ("gotcha", "failure_pattern"):
            content_tokens = _tokenize_text(content_lower)
            error_tokens = _tokenize_text(error_lower)
            overlap_count = len(content_tokens & error_tokens)
            if overlap_count >= 3:
                score += 2.0
            elif overlap_count >= 1:
                score += 0.5

        # Free-text query match
        if query_lower:
            if query_lower in content_lower:
                score += 2.0
            else:
                query_tokens = _tokenize_text(query_lower)
                content_tokens = _tokenize_text(content_lower)
                overlap = query_tokens & content_tokens
                if overlap:
                    score += len(overlap) * 0.5

        # Immutable lifecycle outcomes refine relevance but never authority.
        if score > 0:
            score += min(entry.get("helpful_count", 0) * 0.4, 1.2)
            score -= min(entry.get("not_helpful_count", 0) * 0.5, 1.5)
            score -= min(entry.get("harmful_count", 0) * 1.0, 3.0)
            score -= min(entry.get("superseded_evaluation_count", 0) * 0.75, 2.25)

            confidence = entry.get("confidence", 0.5)
            score += confidence * 0.3

            scored.append((score, entry))

    # Sort by score descending
    scored.sort(key=lambda x: x[0], reverse=True)
    return [entry for _, entry in scored]


def _extract_tags_from_content(content: str) -> list[str]:
    """Extract likely tags from entry content (for migration)."""
    tags = []
    keywords = {
        "pytest": ["pytest", "test_", "fixture", "conftest"],
        "imports": ["import", "from ", "__init__"],
        "contract": ["contract", "ContractViolation", "build_base_context"],
        "spark": ["spark", "Spark", "pyspark", "UNRESOLVED_COLUMN"],
        "pandas": ["pandas", "DataFrame", "is_categorical"],
        "naming": ["naming", "rename", "test_ prefix", "file name"],
        "notebook": ["notebook", "reload", "sys.modules"],
    }
    content_lower = content.lower()
    for tag, patterns in keywords.items():
        if any(p.lower() in content_lower for p in patterns):
            tags.append(tag)
    return tags


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------


def render_memory_report(ctx: dict[str, Any]) -> str:
    """Render memory_context output as markdown."""
    lines = render_header_lines(ctx, "Memory")
    lines.extend(render_metrics_lines(ctx["metrics"]))

    entries = ctx.get("entries", [])
    if entries:
        lines.extend(["", "## Relevant Entries", ""])
        for e in entries:
            type_badge = f"[{e.get('type', '?')}]"
            tags_str = f" `{', '.join(e.get('tags', []))}`" if e.get("tags") else ""
            eid = e.get("id", "?")
            # Show short UUID (8 chars) instead of full
            eid_short = eid[:8] if len(eid) > 8 else eid
            lines.append(f"- **{eid_short}** {type_badge}{tags_str}: {e['content']}")
            if e.get("related_files"):
                lines.append(f"  - Files: {', '.join(f'`{f}`' for f in e['related_files'][:3])}")
    else:
        lines.extend(["", "No relevant entries found.", ""])

    # Associated entries (co-occurrence based)
    associated = ctx.get("associated_entries", [])
    if associated:
        lines.extend(["", "## Associated Memories (by co-occurrence)", ""])
        for a in associated:
            mid = a.get("memory_id", "?")
            mid_short = mid[:8] if len(mid) > 8 else mid
            strength = a.get("strength", 0)
            count = a.get("co_occurrence_count", 0)
            lines.append(f"- **{mid_short}** (strength={strength:.2f}, seen together {count}x)")

    # File annotations (marginalia)
    file_anns = ctx.get("file_annotations", [])
    if file_anns:
        lines.extend(["", "## File Annotations (Marginalia)", ""])
        for a in file_anns:
            loc = a.get("file_path", "?")
            ls = a.get("line_start")
            le = a.get("line_end")
            line_info = f" L{ls}-{le}" if ls else ""
            lines.append(f"- `{loc}{line_info}`: {a['content']}")

    # Table annotations (marginalia)
    table_anns = ctx.get("table_annotations", [])
    if table_anns:
        lines.extend(["", "## Table Annotations (Marginalia)", ""])
        for a in table_anns:
            col = a.get("column_name")
            prefix = f"`{col}` — " if col else ""
            lines.append(f"- {prefix}{a['content']}")

    # Relevant trails (playbooks)
    trails = ctx.get("relevant_trails", [])
    if trails:
        lines.extend(["", "## Relevant Trails (Playbooks)", ""])
        for t in trails:
            lines.append(f"- **{t['name']}**: {t.get('description', '')}")
            for i, step in enumerate(t.get("steps", [])[:6], 1):
                lines.append(f"  {i}. `{step['action']}` → {step.get('outcome', step.get('detail', ''))}")

    lines.extend(render_bullet_section(ctx.get("findings", []), "## Findings"))
    lines.extend(render_bullet_section(ctx.get("risks", []), "## Risks"))
    lines.extend(render_bullet_section(ctx.get("suggested_next_actions", []), "## Next Actions"))

    return "\n".join(lines)
