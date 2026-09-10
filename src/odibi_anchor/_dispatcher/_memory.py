"""_memory.py — Memory dispatch and session generation functions.

Extracted from agent_init.py Phase 4 (revamp spec).
Memory tag queries, memory stats, and session notebook generation.
"""
import os as _os
import json as _json
from datetime import datetime as _datetime, timezone as _timezone

from odibi_anchor.codebase._memory_db import (
    query_memories as _db_query_memories,
    confirm_memory_entry as _db_confirm_entry,
    _DEFAULT_DB_PATH,
)
from odibi_anchor._utils.contract import build_base_context


def _memory_tags(*args, **kwargs) -> dict:
    """Query memory entries filtered by tag(s).

    Lightweight tag-based retrieval. Returns matching entries sorted by recency.
    Supports all auto-tags: file:<path>, task:<type>, protective, recurring,
    time-sink, had-errors, quality:*, long-session, quick-session.

    Usage:
        anchor("memory_tags", tags=["protective"])
        anchor("memory_tags", tags=["file:agent_init.py"])
        anchor("memory_tags", tags=["had-errors", "task:implementation"])
        anchor("memory_tags", tags=["recurring"], limit=5)

    Args:
        tags: List of tags to filter by (all must match — AND semantics).
        limit: Max entries to return (default 10).
        status: Filter by entry status (default: all non-retired).
        output_format: "dict" or "markdown".

    Returns:
        Context dict with matched entries, counts, and tag distribution.
    """
    output_format = kwargs.pop("output_format", "dict")
    db_path = kwargs.get("db_path") or _DEFAULT_DB_PATH
    project = kwargs.get("project")
    tags = kwargs.get("tags") or (list(args) if args else [])
    limit = kwargs.get("limit", 10)
    status_filter = kwargs.get("status", ["candidate", "active", "confirmed"])

    if not tags:
        from odibi_anchor._utils.contract import build_base_context
        return build_base_context(
            kind="memory_tags",
            subject="memory",
            summary="No tags specified — provide tags=['tag1', 'tag2']",
            metrics={"matched": 0, "tags_queried": []},
            findings=["Usage: anchor('memory_tags', tags=['protective'])"],
            risks=[],
            samples={},
            suggested_next_actions=[
                "Try: anchor('memory_tags', tags=['protective'])",
                "Try: anchor('memory_tags', tags=['file:bootstrap.py'])",
                "Try: anchor('memory_tags', tags=['recurring'])",
            ],
        )
    if not project:
        raise ValueError("memory_tags requires an active project/trust boundary")

    # Apply project scope before returning any content.
    results = _db_query_memories(
        db_path,
        project=project,
        tags=tags if isinstance(tags, list) else [tags],
        status=status_filter,
        limit=limit,
    )

    # Build response
    from odibi_anchor._utils.contract import build_base_context
    entries_summary = []
    for e in results:
        entries_summary.append({
            "id": e.get("id", ""),
            "type": e.get("type", ""),
            "content": e.get("content", "")[:200],
            "tags": e.get("tags", []),
            "confidence": e.get("confidence", 0),
            "status": e.get("status", ""),
        })

    summary = f"Found {len(results)} entries matching tags {tags}"

    ctx = build_base_context(
        kind="memory_tags",
        subject="memory",
        summary=summary,
        metrics={
            "matched": len(results),
            "tags_queried": tags,
            "limit": limit,
        },
        findings=[f"{len(results)} entries match {tags}"] if results else [f"No entries match {tags}"],
        risks=[],
        samples={"entries": entries_summary},
        suggested_next_actions=[
            "Keep candidates advisory and verify them against current evidence; retrieval, "
            "application, evaluation, and counters do not promote. Use a typed verifier or "
            "separate authenticated owner activation and confirmation requests for authority.",
            "Run anchor('memory_tags', tags=['protective']) to see error-preventing entries.",
        ],
    )

    if output_format == "markdown":
        md_lines = [f"# Memory Tags Query: {tags}\n"]
        md_lines.append(f"**{summary}**\n")
        if entries_summary:
            md_lines.append("| Type | Content | Tags | Status |")
            md_lines.append("| --- | --- | --- | --- |")
            for e in entries_summary:
                content_short = e["content"][:80].replace("|", "\\|")
                tags_short = ", ".join(e["tags"][:4]) if isinstance(e["tags"], list) else str(e["tags"])[:40]
                md_lines.append(f"| {e['type']} | {content_short} | {tags_short} | {e['status']} |")
        else:
            md_lines.append("No matching entries found.")
        md_lines.append(f"\n*Query: tags={tags}, limit={limit}*")
        return "\n".join(md_lines)
    return ctx




def _auto_confirm_surfaced(result: dict | str, db_path: str | None = None) -> None:
    """Compatibility no-op: exposure is telemetry, never confirmation evidence."""
    return None




def _auto_confirm_eligible(db_path: str | None = None) -> int:
    """Compatibility no-op: successful gates do not confirm fetched memories."""
    return 0




def _memory_stats(
    db_path: str | None = None, output_format: str = "markdown", *, project: str | None = None,
    **_kwargs,
) -> dict | str:
    """Return status/type/project counts and explicit evidence leaders."""
    if not project:
        raise ValueError("memory_stats requires an active project/trust boundary")
    target_db = db_path or _DEFAULT_DB_PATH
    entries = _db_query_memories(
        target_db, project=project,
        status=["candidate", "active", "confirmed", "rejected", "superseded", "stale",
                "quarantined", "retired"],
        limit=None,
    )
    by_status: dict[str, int] = {}
    by_type: dict[str, int] = {}
    by_project: dict[str, int] = {}
    for entry in entries:
        by_status[entry["status"]] = by_status.get(entry["status"], 0) + 1
        by_type[entry["type"]] = by_type.get(entry["type"], 0) + 1
        by_project[entry["project"]] = by_project.get(entry["project"], 0) + 1
    leaders = sorted(
        entries,
        key=lambda entry: (
            entry["helpful_count"], entry["application_count"], entry.get("created", ""),
        ),
        reverse=True,
    )[:10]
    top_used_list = [{
        "id": entry["id"], "type": entry["type"], "status": entry["status"],
        "application_count": entry["application_count"],
        "helpful_count": entry["helpful_count"],
        "negative_count": entry["not_helpful_count"] + entry["harmful_count"]
        + entry["superseded_evaluation_count"],
        "confidence": entry["confidence"], "content_preview": entry["content"][:80],
    } for entry in leaders]

    total = sum(by_status.values())

    from odibi_anchor._utils.contract import build_base_context, finalize_context

    ctx = build_base_context(
        kind="memory_stats",
        subject="agent_memory",
        summary=f"Memory DB: {total} entries; authority requires explicit evidence",
        metrics={"total": total},
        findings=[f"{status}: {cnt}" for status, cnt in by_status.items()],
        risks=[],
        samples={},
        suggested_next_actions=[
            "MUST: Run anchor('archive') to clean stale entries" if total > 100 else "",
        ],
        by_status=by_status,
        by_type=by_type,
        by_project=by_project,
        project=project,
        top_by_explicit_evidence=top_used_list,
    )
    # Remove empty suggestions
    ctx["suggested_next_actions"] = [a for a in ctx["suggested_next_actions"] if a]
    if output_format == "markdown":
        return _render_memory_stats(ctx)
    return ctx




def _render_memory_stats(ctx: dict) -> str:
    """Render memory_stats dict as a concise markdown report."""
    total = ctx.get("total") or ctx.get("metrics", {}).get("total", 0)
    lines = [f"## Memory Stats ({total} entries)\n"]

    # Status breakdown
    lines.append("### By Status")
    for status, cnt in ctx["by_status"].items():
        lines.append(f"- **{status}**: {cnt}")

    # Type breakdown
    lines.append("\n### By Type")
    for entry_type, cnt in ctx["by_type"].items():
        lines.append(f"- {entry_type}: {cnt}")

    # Project breakdown
    if len(ctx["by_project"]) > 1:
        lines.append("\n### By Project")
        for proj, cnt in ctx["by_project"].items():
            lines.append(f"- {proj}: {cnt}")

    lines.append("\n### Top Entries by Explicit Evidence")
    lines.append("| ID | Type | Status | Applications | Helpful | Negative | Conf | Preview |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for e in ctx["top_by_explicit_evidence"][:10]:
        eid = e["id"][:8]
        lines.append(
            f"| {eid} | {e['type']} | {e['status']} | {e['application_count']} | "
            f"{e['helpful_count']} | {e['negative_count']} "
            f"| {e['confidence']} | {e['content_preview'][:50]}{'...' if len(e.get('content_preview', '')) > 50 else ''} |"
        )

    return "\n".join(lines)






def _new_session(
    root,
    anchor_root,
    *args,
    artifact_root=None,
    project: str | None = None,
    **kwargs,
) -> dict:
    """Generate a pre-structured session notebook with enforcement scaffolding.

    Creates a notebook beneath the active project's durable artifact root when
    available, otherwise at anchor_root/sessions/{project}/YYYY-MM-DD_{name}.ipynb.
    with pre-built cells for the mandatory workflow sequence. Each feature block
    includes: task → known_bad → work zone → gate → learn.

    Args:
        name: Required. Session/feature name (e.g. "refactor_gate_logic").
        features: Number of feature blocks to generate (default 3).
        project: Active managed project ID to restore from the generated notebook.

    Inline mode is the default and configures the logical session without creating a
    file. Pass ``inline=False`` only when a durable notebook is intentionally needed.

    Returns:
        Contract dict describing the inline session or created notebook.

    Usage:
        anchor("new_session", name="add_drift_detection")  # inline, no file
        anchor("new_session", name="multi_feature_sprint", features=5, inline=False)
    """
    name = kwargs.pop("name", args[0] if args else None)
    features = kwargs.pop("features", 3)

    if not name or len(str(name).strip()) < 3:
        raise RuntimeError(
            "BLOCKED: anchor(\"new_session\") requires name= (≥3 chars).\n"
            "Usage: anchor(\"new_session\", name=\"feature_name\")"
        )

    name = str(name).strip().replace(" ", "_").lower()
    inline = kwargs.pop("inline", True)

    # ── Inline mode: configure current session without creating notebook ──
    if inline:
        from odibi_anchor._utils._session_state import _SESSION_TIMINGS
        from odibi_anchor._utils.contract import build_base_context

        return build_base_context(
            kind="new_session",
            subject=name,
            summary=f"Session '{name}' configured inline ({features} features planned).",
            metrics={
                "session_name": name,
                "features_planned": features,
                "mode": "inline",
                "notebook_created": False,
            },
            findings=[
                f"Session named: '{name}'",
                f"Features planned: {features}",
                "Inline mode — no notebook created. Current context is your session.",
            ],
            risks=[],
            samples={},
            suggested_next_actions=[
                f"Run anchor('task', ...) for your first feature.",
                "Use anchor('checkpoint', label='feature_N') between features.",
                "Close with structured learning assessment, then run anchor('snapshot').",
            ],
        )

    # Resolve project name from root
    _project_name = project or _os.path.basename(str(root))
    _sessions_dir = (
        _os.path.join(str(artifact_root), "notebooks")
        if artifact_root
        else _os.path.join(anchor_root, "sessions", _project_name)
    )
    _os.makedirs(_sessions_dir, exist_ok=True)

    _date = _datetime.now(_timezone.utc).strftime("%Y-%m-%d")
    _nb_name = f"{_date}_{name}.ipynb"
    _nb_path = _os.path.join(_sessions_dir, _nb_name)

    def _md_cell(source_lines):
        return {"cell_type": "markdown", "metadata": {}, "source": source_lines}

    def _code_cell(source_lines):
        return {"cell_type": "code", "metadata": {}, "source": source_lines,
                "outputs": [], "execution_count": None}

    cells = []

    # ── Header section (runs once) ──
    cells.append(_md_cell([
        f"# Session: {name}\n",
        f"**Project:** {_project_name}  \n",
        f"**Date:** {_date}  \n",
        f"**Features planned:** {features}\n",
        "\n",
        "---\n",
        "## Mandatory Sequence\n",
        "1. Bootstrap → Status → Audit History → New Session → Task → Bounded Task Memory\n",
        "2. Required Skills → Known Bad → Edit → Review/Test/Gate → Assessment\n",
        "3. All enforced with RuntimeError — cells MUST run in order.\n",
    ]))

    cells.append(_md_cell(["### Bootstrap"]))
    cells.append(_code_cell([
        "from odibi_anchor.bootstrap import init\n",
        f"anchor, ROOT, MANIFEST = init(root={str(root)!r}, project={project!r})",
    ]))

    cells.append(_md_cell(["### Orientation"]))
    cells.append(_code_cell(['status_result = anchor("status")']))
    cells.append(_code_cell(['audit_result = anchor("audit_history")']))
    cells.append(_code_cell([
        f'session_result = anchor("new_session", name={name!r}, features={features}, inline=True)'
    ]))

    # ── Feature blocks ──
    for i in range(1, features + 1):
        cells.append(_md_cell([
            f"---\n",
            f"## Feature {i}: [DESCRIBE FEATURE HERE]\n",
        ]))

        cells.append(_md_cell(["### Planning"]))
        cells.append(_code_cell([
            f'task_result = anchor("task", "[describe what you are doing]",\n',
            f'    goal="[intended outcome]",\n',
            f'    mode="implementation",  # or: debugging, analysis, migration\n',
            f'    acceptance_criteria=["[state how completion will be verified]"]\n',
            f')',
        ]))
        cells.append(_md_cell([
            "### Bounded Task Memory\n",
            "Review `task_result['memory_context']` now. Acknowledge each selection or none; "
            "do not scan the full store or force use.\n",
        ]))
        cells.append(_md_cell([
            "### Task-Derived Requirements\n",
            "Read every `SKILL.md` path in `required_skills`, then run the registration cell. "
            "Persist/review a Spec only when the accepted task policy requires one.\n",
        ]))
        cells.append(_code_cell([
            'required_skills = task_result.get("required_skills", [])\n',
            'required_skills  # read every listed SKILL.md before continuing',
        ]))
        cells.append(_code_cell([
            '# Run only after reading every listed SKILL.md.\n',
            'skill_results = [anchor("skill_loaded", item["skill"]) for item in required_skills]',
        ]))

        cells.append(_md_cell(["### Pre-Edit Checks"]))
        cells.append(_code_cell([
            'known_bad_result = anchor("known_bad", changed_files=[\n',
            '    # list .py files you plan to edit\n',
            '])',
        ]))

        cells.append(_md_cell([
            "### Work Zone\n",
            "Add as many cells as needed below. Each edit should call `anchor(\"touched\", \"path\")`.\n",
        ]))
        cells.append(_code_cell(["# Your work here\n"]))
        cells.append(_code_cell(["# More work cells as needed\n"]))

        cells.append(_md_cell(["### Delivery"]))
        cells.append(_code_cell(['preflight_result = anchor("preflight")  # required for Python changes']))
        cells.append(_code_cell(['test_result = anchor("test")  # required for Python changes']))
        cells.append(_code_cell(['review_result = anchor("review")']))
        cells.append(_code_cell(['gate_result = anchor("gate")']))
        cells.append(_md_cell([
            "### Learning Assessment\n",
            "Choose exactly one truthful route. The executable default records no learning. "
            "If reusable learning exists, replace it by capturing genuine observations and "
            "assessing their returned IDs as shown in the comments.\n",
        ]))
        cells.append(_code_cell([
            'learning_result = anchor("learning", "assess", outcome="nothing_reusable_learned")\n',
            '# Real-learning alternative (replace the call above; never submit placeholders):\n',
            '# observation = anchor("learning", "capture", ...)\n',
            '# learning_result = anchor("learning", "assess", outcome="observations_recorded",\n',
            '#     observation_ids=[observation["item"]["item_id"]])',
        ]))

    # ── Notebook metadata ──
    notebook = {
        "cells": cells,
        "metadata": {
            "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
            "language_info": {"name": "python", "version": "3.10.0"},
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    }

    with open(_nb_path, "w", encoding="utf-8") as f:
        _json.dump(notebook, f, indent=1)
        f.write("\n")

    from odibi_anchor._utils.contract import build_base_context
    return build_base_context(
        kind="new_session",
        subject=name,
        summary=f"Session notebook created: {_nb_name} ({len(cells)} cells, {features} feature blocks)",
        metrics={
            "notebook_created": True,
            "mode": "notebook",
            "notebook_path": _nb_path,
            "cells": len(cells),
            "feature_blocks": features,
            "project": _project_name,
        },
        findings=[f"Created: {_nb_path}"],
        risks=[],
        samples={},
        suggested_next_actions=[
            f"Open {_nb_path} and run cells in order.",
            "Fill in the task and close each gate with explicit structured assessment.",
        ],
    )


from odibi_anchor._dispatcher._session_tools import _format_test_result


def _append_notebook_cells(notebook_path: str, cells: list[dict]) -> bool:
    """Append cells to an existing session notebook.

    Reads the notebook JSON, appends the cells before the final metadata,
    and writes it back. Returns True on success, False on any failure.
    """
    try:
        with open(notebook_path, "r", encoding="utf-8") as f:
            nb = _json.load(f)
        nb.setdefault("cells", []).extend(cells)
        with open(notebook_path, "w", encoding="utf-8") as f:
            _json.dump(nb, f, indent=1)
            f.write("\n")
        return True
    except Exception:
        return False  # SILENT-OK: notebook append is best-effort


def _md_cell(source_lines: list[str]) -> dict:
    """Create a markdown cell for notebook injection."""
    return {"cell_type": "markdown", "metadata": {}, "source": source_lines}


def _code_cell(source_lines: list[str]) -> dict:
    """Create a code cell for notebook injection."""
    return {"cell_type": "code", "metadata": {}, "source": source_lines,
            "outputs": [], "execution_count": None}


def append_gate_to_notebook(
    notebook_path: str,
    gate_result: dict,
    files_changed: set[str],
    feature_num: int | None = None,
) -> bool:
    """Append gate results as cells to the session notebook."""
    if not notebook_path:
        return False

    ts = _datetime.now(_timezone.utc).strftime("%H:%M UTC")
    metrics = gate_result.get("metrics", {})
    passed = metrics.get("all_verified", False)
    status = "✅ PASSED" if passed else "⚠️ PARTIAL"
    label = f"Feature {feature_num}" if feature_num else "Gate"

    cells = [
        _md_cell([
            f"---\n",
            f"### {label} — {status} ({ts})\n",
        ]),
        _md_cell([
            f"**Files changed:** {len(files_changed)}  \n",
            *[f"- `{f}`\n" for f in sorted(files_changed)[:15]],
        ]),
        _md_cell([
            f"**Score:** {metrics.get('score', '?')}/{metrics.get('max_score', '?')}  \n",
            f"**Rating:** {metrics.get('rating', '?')}  \n",
            *[f"- {f}\n" for f in gate_result.get("findings", [])[:10]],
        ]),
    ]

    return _append_notebook_cells(notebook_path, cells)


def append_learn_to_notebook(
    notebook_path: str,
    learn_result: dict,
    session_events: list[dict] | None = None,
) -> bool:
    """Append learn results as cells to the session notebook."""
    if not notebook_path:
        return False

    ts = _datetime.now(_timezone.utc).strftime("%H:%M UTC")
    lines = [f"### Learnings ({ts})\n", "\n"]

    if session_events:
        for evt in session_events[:10]:
            if isinstance(evt, dict):
                etype = evt.get("type", "note")
                detail = evt.get("detail", evt.get("content", ""))
                if detail:
                    lines.append(f"- **{etype}:** {detail}\n")

    metrics = learn_result.get("metrics", {})
    added = metrics.get("memories_added", 0)
    if added:
        lines.append(f"\n*{added} memory entries added.*\n")

    return _append_notebook_cells(notebook_path, [_md_cell(lines)])


def append_assessment_to_notebook(
    notebook_path: str, assessment_id: str, observation_ids: list[str],
) -> bool:
    """Append identifier-only structured closure bookkeeping."""
    if not notebook_path:
        return False
    lines = ["### Structured assessment\n", "\n", f"- assessment: `{assessment_id}`\n"]
    lines.extend(f"- observation: `{item_id}`\n" for item_id in observation_ids)
    return _append_notebook_cells(notebook_path, [_md_cell(lines)])
