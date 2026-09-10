"""_frame.py — Context Frame and contextual suggestion functions.

Extracted from agent_init.py Phase 5 (revamp spec).
Session frame reading and frame-derived suggestion injection.
"""
from odibi_anchor._utils.contract import build_base_context
from odibi_anchor._utils._context_frame import ContextFrame as _ContextFrame


def _build_contextual_suggestions(frame: "_ContextFrame", result: dict) -> list[str]:
    """Inject frame-derived contextual suggestions into a tool result.

    Called after every anchor() call that returns a StandardContract dict.
    Inspects accumulated frame state and emits specific, actionable
    suggestions — e.g. "run microscope on created_date — 15% nulls found"
    instead of the generic "SHOULD: run microscope on suspicious columns".

    Design principles:
        - Never raises — all exceptions silently swallowed
        - <5ms — pure frame inspection, no I/O, no DB queries
        - Append-only — only adds suggestions, never removes existing ones
        - Deduplicated — checks existing suggestions before adding
    """
    suggestions: list[str] = []
    try:
        existing = set(result.get("suggested_next_actions", []))

        # ── Profile findings → specific microscope suggestions ──
        # If profiler found null anomalies on specific columns, suggest microscope with column name
        null_subjects: list[str] = []
        for f in frame.findings:
            if f.source in ("profile_table",) and (
                "null" in f.message.lower() or "missing" in f.message.lower()
            ):
                # Extract column from message patterns like "X% null rate on col" or "col: 15% nulls"
                import re as _re
                col_match = _re.search(
                    r"(?:on |column[:\s]+|\bfield[:\s]+)([\w.]+)",
                    f.message, _re.IGNORECASE
                )
                col_name = col_match.group(1) if col_match else f.subject.split(".")[-1] if f.subject else None
                if col_name and col_name not in null_subjects:
                    null_subjects.append(col_name)
                    sug = f"SHOULD: anchor('microscope', df, '{col_name}') — profiler found null anomaly"
                    if sug not in existing:
                        suggestions.append(sug)

        # ── Profile findings → freshness suggestion if stale ──
        for f in frame.findings:
            if f.source in ("profile_table",) and (
                "stale" in f.message.lower() or "freshness" in f.message.lower()
            ):
                table = f.subject or "this table"
                sug = f"SHOULD: anchor('case_file', df, filter='where:date_col IS NULL') — freshness issue on {table}"
                if sug not in existing:
                    suggestions.append(sug)
                    break  # One freshness suggestion is enough

        # ── Memory gotchas → specific review warnings for changed files ──
        for fp in frame.code_context.files_changed:
            if frame.memory_context.has_gotcha_for(fp):
                sug = (
                    f"MUST: Reconcile the bounded task-memory gotcha for {fp} "
                    "with current evidence before editing"
                )
                if sug not in existing:
                    suggestions.append(sug)

        # ── Data context → redundancy hints ──
        # If subject already profiled, tell the agent they can skip re-profiling
        result_subject = result.get("subject", "")
        if result_subject and frame.data_context.has_profile(result_subject):
            action_from_result = result.get("kind", "")
            if action_from_result in ("profile_table", "profile_table_context", "table_profile"):
                sug = f"NOTE: {result_subject} already profiled this session — frame has prior findings"
                if sug not in existing:
                    suggestions.append(sug)

        # ── Validation blockers → MUST-fix suggestions ──
        for f in frame.findings:
            if f.source == "validate" and f.severity == "critical":
                sug = f"MUST: Fix validation blocker on {f.subject} before promotion — {f.message}"
                if sug not in existing:
                    suggestions.append(sug)
                    existing.add(sug)

        # ── Risk escalation → gate reminder when high risks accumulate ──
        high_risk_count = sum(1 for r in frame.risks if r.severity == "high")
        if high_risk_count >= 3:
            sug = f"MUST: anchor('gate') — {high_risk_count} high-severity risks accumulated this session"
            if sug not in existing:
                suggestions.append(sug)

    except Exception as _exc:
        from odibi_anchor._utils._session_state import record_degraded
        record_degraded("frame_suggestion_injection", _exc)

    return suggestions




def _frame_action(session_frame, anchor_frame_enabled, *args, **kwargs) -> dict | str:
    """Return the current session Context Frame as a summary.

    Usage:
        anchor("frame")                      # Full frame summary
        anchor("frame", section="data")      # Just data context (tables profiled, etc.)
        anchor("frame", section="risks")     # Just active risks
        anchor("frame", section="actions")   # Action log
        anchor("frame", section="findings")  # All findings
        anchor("frame", section="code")      # Code context
        anchor("frame", section="memory")    # Memory context
        anchor("frame", section="compliance")# Compliance context
        anchor("frame", query="null")        # Search findings for keyword
    """
    from odibi_anchor._utils.contract import build_base_context

    output_format = kwargs.pop("output_format", "markdown")
    section = kwargs.get("section")
    query = kwargs.get("query")

    if session_frame is None:
        ctx = build_base_context(
            kind="context_frame",
            subject="session",
            summary="Context Frame: not initialized (anchor_frame_enabled may be False)",
            metrics={"enabled": anchor_frame_enabled, "initialized": False},
            findings=["Frame not initialized — bootstrap may have run with anchor_frame_enabled=False"],
            risks=[],
            samples={},
            suggested_next_actions=["Re-run bootstrap to initialize the frame."],
        )
        if output_format == "markdown":
            return "# Context Frame\n\n**Not initialized.**"
        return ctx

    frame = session_frame

    # ── Section mode: return one sub-context ──
    if section:
        section_data = frame.get_section(section)
        count = len(section_data.get(section, section_data.get("findings", section_data.get("risks", section_data.get("actions", [])))))
        ctx = build_base_context(
            kind="context_frame",
            subject=f"frame.{section}",
            summary=f"Context Frame section '{section}': {count} item(s)",
            metrics={"section": section, "count": count, "total_findings": len(frame.findings)},
            findings=[],
            risks=[],
            samples={"section_data": section_data},
            suggested_next_actions=["anchor(\'frame\') for full summary"],
        )
        if output_format == "markdown":
            import json as _json
            lines = [f"# Context Frame — {section}\n"]
            lines.append(f"**{ctx['summary']}**\n")
            lines.append("```json")
            lines.append(_json.dumps(section_data, indent=2, default=str))
            lines.append("```")
            return "\n".join(lines)
        return ctx

    # ── Query mode: search findings ──
    if query:
        matched = frame.search_findings(query)
        findings_msgs = [f"{f.source}: [{f.category}/{f.severity}] {f.message}" for f in matched]
        ctx = build_base_context(
            kind="context_frame",
            subject=f"frame.search:{query}",
            summary=f"Context Frame search '{query}': {len(matched)} finding(s) matched",
            metrics={"query": query, "matched": len(matched), "total_findings": len(frame.findings)},
            findings=findings_msgs,
            risks=[],
            samples={},
            suggested_next_actions=["anchor(\'frame\') for full summary"],
        )
        if output_format == "markdown":
            lines = [f"# Context Frame — Search: {query!r}\n"]
            lines.append(f"**{len(matched)} matching finding(s)**\n")
            for msg in findings_msgs:
                lines.append(f"- {msg}")
            return "\n".join(lines)
        return ctx

    # ── Full summary mode ──
    by_cat = frame.findings_by_category()
    by_src = frame.findings_by_source()
    risk_counts = frame.risks_by_severity()
    active_risks = [r.message for r in frame.risks if r.severity in ("high", "medium")][:5]
    tables_profiled = list(frame.data_context.tables_profiled.keys())
    files_touched = sorted(frame.code_context.files_changed)
    gates_passed = frame.compliance.gates_passed

    summary = (
        f"Context Frame [{frame.session_id}]: "
        f"{len(frame.findings)} findings, {len(frame.risks)} risks, "
        f"{len(frame.actions)} actions | "
        f"project={frame.project!r}"
    )

    ctx = build_base_context(
        kind="context_frame",
        subject="session",
        summary=summary,
        metrics={
            "session_id": frame.session_id,
            "project": frame.project,
            "total_findings": len(frame.findings),
            "total_risks": len(frame.risks),
            "total_actions": len(frame.actions),
            "total_decisions": len(frame.decisions),
            "findings_by_category": by_cat,
            "findings_by_source": by_src,
            "risks_by_severity": risk_counts,
            "tables_profiled": len(tables_profiled),
            "files_in_code_context": len(files_touched),
            "gates_passed": gates_passed,
            "tools_used": list(frame.tool_results.keys()),
            "frame_age_s": round(
                (frame.last_updated - frame.created).total_seconds(), 1
            ),
        },
        findings=[
            f"{cat}: {cnt} finding(s)" for cat, cnt in sorted(by_cat.items(), key=lambda x: -x[1])
        ],
        risks=active_risks,
        samples={
            "tables_profiled": tables_profiled,
            "files_changed": files_touched,
            "gates_passed": gates_passed,
        },
        suggested_next_actions=[
            "anchor(\'frame\', section=\'data\') — inspect profiled tables",
            "anchor(\'frame\', section=\'risks\') — review active risks",
            "anchor(\'frame\', query=\'null\') — search findings by keyword",
        ],
    )

    if output_format == "markdown":
        lines = ["# Context Frame\n"]
        lines.append(f"**{summary}**\n")
        lines.append("## Findings by Category\n")
        if by_cat:
            lines.append("| Category | Count |")
            lines.append("| --- | --- |")
            for cat, cnt in sorted(by_cat.items(), key=lambda x: -x[1]):
                lines.append(f"| {cat} | {cnt} |")
        else:
            lines.append("_No findings recorded yet._")
        if active_risks:
            lines.append("\n## Active Risks\n")
            for r in active_risks:
                lines.append(f"- {r}")
        if tables_profiled:
            lines.append("\n## Data Context\n")
            lines.append(f"Tables profiled: {', '.join(tables_profiled)}")
        if files_touched:
            lines.append("\n## Code Context\n")
            for fp in files_touched:
                lines.append(f"- {fp}")
        if gates_passed:
            lines.append("\n## Compliance Gates Passed\n")
            for g in gates_passed:
                lines.append(f"- ✓ {g}")
        lines.append("\n## Next Actions\n")
        for a in ctx["suggested_next_actions"]:
            lines.append(f"- {a}")
        return "\n".join(lines)
    return ctx


