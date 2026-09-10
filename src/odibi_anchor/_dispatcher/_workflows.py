"""_workflows.py — Composed high-level data workflows.

Phase 1 (MVP): workflow engine helpers + onboard workflow.
See specs/COMPOSE_SPEC.md for full design.

Design principles:
  1. Adaptive, not scripted — steps skip when not needed.
  2. Context threading — each step feeds the next.
  3. Fail-forward — one failure does not abort the workflow.
  4. Transparent — steps array shows exactly what ran.
  5. Direct tool calls — no anchor() re-dispatch.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from odibi_anchor._utils.contract import build_base_context
from odibi_anchor._utils.engine_utils import detect_engine


# ─── Workflow Engine Helpers ──────────────────────────────────────────────────



def _guard_empty_df(df, workflow_name: str) -> dict | None:
    """Return early-exit context if df is empty, else None.

    Called at top of each workflow, after input resolution, before any _run_step.
    """
    if df is not None and hasattr(df, '__len__') and len(df) == 0:
        cols = list(df.columns) if hasattr(df, 'columns') else []
        return build_base_context(
            kind=f"workflow_{workflow_name}",
            subject="empty_input",
            summary=f"Input DataFrame has 0 rows — {workflow_name} cannot proceed",
            metrics={"row_count": 0, "column_count": len(cols), "columns": cols},
            findings=[
                f"Input DataFrame has 0 rows. Provide a non-empty DataFrame to anchor('{workflow_name}', ...).",
                f"Tip: Check upstream filters or joins that may have eliminated all rows.",
            ],
            suggested_next_actions=[
                f"Verify upstream data: check filters, joins, or WHERE clauses that produced this DataFrame.",
                f"Use anchor('help', '{workflow_name}') to see required parameters.",
                f"If the empty result is expected, no action needed.",
            ],
        )
    return None


def _single_row_warning(df) -> str | None:
    """Return a warning string if df has exactly 1 row, else None."""
    if df is not None and hasattr(df, '__len__') and len(df) == 1:
        return "⚠ Input has only 1 row — statistics and distributions may be unreliable"
    return None


def _detect_all_null_columns(df) -> list[str]:
    """Return list of column names that are entirely NULL/NaN."""
    if df is None or not hasattr(df, 'columns'):
        return []
    all_null = []
    for col in df.columns:
        try:
            if df[col].isna().all():
                all_null.append(col)
        except (TypeError, AttributeError):
            pass
    return all_null


def _run_step(
    tool_name: str,
    fn: Callable[..., dict],
    *args: Any,
    **kwargs: Any,
) -> dict:
    """Execute a single workflow step, capturing timing and errors.

    Returns a step log entry dict with keys:
        tool, status, duration_s, summary, result (full tool output on success).
    On error: status="error", error=str(exception), result=None.
    """
    t0 = time.perf_counter()
    try:
        result = fn(*args, **kwargs)
        elapsed = time.perf_counter() - t0
        summary = result.get("summary", "") if isinstance(result, dict) else ""
        return {
            "tool": tool_name,
            "status": "ok",
            "duration_s": round(elapsed, 2),
            "summary": summary,
            "result": result,
        }
    except Exception as exc:
        elapsed = time.perf_counter() - t0
        return {
            "tool": tool_name,
            "status": "error",
            "duration_s": round(elapsed, 2),
            "summary": f"{type(exc).__name__}: {exc}",
            "error": str(exc),
            "result": None,
        }


def _merge_findings(*step_results: dict | None) -> list:
    """Merge and deduplicate findings from multiple step results."""
    seen: set[str] = set()
    merged: list = []
    for result in step_results:
        if result is None:
            continue
        for finding in result.get("findings", []):
            # Findings can be strings or dicts — use str repr for dedup
            key = finding if isinstance(finding, str) else str(finding)
            if key not in seen:
                seen.add(key)
                merged.append(finding)
    return merged


def _merge_risks(*step_results: dict | None) -> list:
    """Merge and deduplicate risks from multiple step results."""
    seen: set[str] = set()
    merged: list = []
    for result in step_results:
        if result is None:
            continue
        for risk in result.get("risks", []):
            key = risk if isinstance(risk, str) else str(risk)
            if key not in seen:
                seen.add(key)
                merged.append(risk)
    return merged


def _build_workflow_result(
    kind: str,
    subject: str,
    steps: list[dict],
    *,
    extra_metrics: dict | None = None,
    extra_samples: dict | None = None,
    output_format: str = "dict",
) -> dict | str:
    """Assemble the final workflow contract from step outputs."""
    step_results = [s.get("result") for s in steps if s.get("result")]
    findings = _merge_findings(*step_results)
    risks = _merge_risks(*step_results)

    ok_count = sum(1 for s in steps if s["status"] == "ok")
    error_count = sum(1 for s in steps if s["status"] == "error")
    skipped = [s["tool"] for s in steps if s["status"] == "skipped"]
    total_duration = sum(s.get("duration_s", 0) for s in steps)

    metrics: dict[str, Any] = {
        "steps_ok": ok_count,
        "steps_error": error_count,
        "steps_skipped": len(skipped),
        "total_duration_s": round(total_duration, 2),
    }
    if extra_metrics:
        metrics.update(extra_metrics)

    # Build summary from first successful step's row count if available
    first_result = step_results[0] if step_results else {}
    row_count = first_result.get("metrics", {}).get("row_count", "?")
    col_count = first_result.get("metrics", {}).get("column_count", "?")
    summary_parts = [f"{row_count} rows x {col_count} cols"]
    if error_count:
        summary_parts.append(f"{error_count} step(s) errored")
    if findings:
        summary_parts.append(f"{len(findings)} finding(s)")

    summary = f"{kind.replace('workflow_', '').capitalize()}ed {subject}: {' | '.join(summary_parts)}"

    # Suggested next actions from the last successful step, plus workflow-level
    suggested: list[str] = []
    for r in reversed(step_results):
        if r and isinstance(r, dict):
            sna = r.get("suggested_next_actions")
            if isinstance(sna, list) and sna:
                suggested = sna[:3]
                break

    # NOTE: do not duplicate `steps` into samples — it is already emitted at the
    # top level (ctx["steps"]) where chain() reads it. Embedding the full step
    # results twice roughly doubled the payload and pushed composed workflows
    # (investigate) past the MCP response-size limit.
    samples: dict[str, Any] = extra_samples or {}
    samples["skipped"] = skipped

    ctx = build_base_context(
        kind=kind,
        subject=subject,
        summary=summary,
        metrics=metrics,
        findings=findings,
        risks=risks,
        samples=samples,
        suggested_next_actions=suggested,
    )
    ctx["steps"] = steps
    ctx["skipped"] = skipped

    if output_format == "markdown":
        return _render_workflow_md(ctx)
    return ctx


def _render_workflow_md(ctx: dict) -> str:
    """Render a workflow context dict as markdown."""
    lines: list[str] = []
    lines.append(f"# {ctx['kind']}")
    lines.append(f"**Subject:** {ctx['subject']}")
    lines.append(f"**Summary:** {ctx['summary']}\n")

    # Metrics
    lines.append("## Metrics")
    for k, v in ctx.get("metrics", {}).items():
        lines.append(f"- {k}: {v}")
    lines.append("")

    # Steps
    lines.append("## Steps")
    for step in ctx.get("steps", []):
        icon = "V" if step["status"] == "ok" else "X" if step["status"] == "error" else "S"
        detail = step.get('summary', step.get('reason', ''))
        line = f"[{icon}] **{step['tool']}** ({step.get('duration_s', 0):.1f}s) - {detail}"
        lines.append(f"- {line}")
    lines.append("")

    # Findings
    if ctx.get("findings"):
        lines.append("## Findings")
        for f in ctx["findings"]:
            lines.append(f"- {f}")
        lines.append("")

    # Risks
    if ctx.get("risks"):
        lines.append("## Risks")
        for r in ctx["risks"]:
            lines.append(f"- {r}")
        lines.append("")

    # Suggested
    if ctx.get("suggested_next_actions"):
        lines.append("## Suggested Next Actions")
        for a in ctx["suggested_next_actions"]:
            lines.append(f"- {a}")

    return "\n".join(lines)


# ─── Onboard Workflow ─────────────────────────────────────────────────────────


def _onboard_workflow(
    *args: Any,
    subject: str | None = None,
    level: str = "standard",
    output_format: str = "dict",
    **kwargs: Any,
) -> dict | str:
    """Onboard workflow — answers 'What IS this table?'

    Sequence:
        profile_table -> suggest_rules -> validate (if rules) -> microscope (if issues)

    Args:
        *args: First positional is table name (str) or DataFrame.
        subject: Human label. Inferred from table name if str.
        level: Profiling level ("standard" or "deep").
        output_format: "dict" or "markdown".

    Returns:
        Workflow contract dict or markdown string.
    """
    from tools.table_profiler_tool.lib.profiler import profile_table
    from tools.table_profiler_tool.lib.contract import serialize_profile
    from tools.table_profiler_tool.lib.microscope import microscope
    from tools.table_profiler_tool.lib.contract import serialize_microscope
    from tools.suggest_rules_tool.suggest_rules_impl import suggest_rules_context
    from odibi_anchor.validation.validation_summary_context import (
        validation_summary_context,
    )

    # ── Resolve input ──
    target = args[0] if args else None
    if target is None:
        return build_base_context(
            kind="workflow_onboard",
            subject="error",
            summary="No table or DataFrame provided",
            metrics={},
            findings=["Usage: anchor('investigate', 'catalog.schema.table', mode='onboard') or anchor('investigate', df, subject='name', mode='onboard')"],
            suggested_next_actions=[
                "Run anchor('help', 'investigate') to see usage and parameters.",
                "Check that all required arguments are provided.",
            ],
        )

    if isinstance(target, str):
        # Table name — read via Spark
        try:
            from pyspark.sql import SparkSession

            spark = SparkSession.getActiveSession()
            df = spark.table(target)
        except Exception as exc:
            return build_base_context(
                kind="workflow_onboard",
                subject=target,
                summary=f"Failed to read table: {exc}",
                metrics={},
                findings=[f"spark.table('{target}') failed: {exc}"],
            )
        if subject is None:
            subject = target
    else:
        df = target
        if subject is None:
            subject = "dataframe"

    # ── Empty input guard ──
    guard = _guard_empty_df(df, "onboard")
    if guard is not None:
        return guard

    # ── Single-row and all-null warnings ──
    _sr_warn = _single_row_warning(df)

    steps: list[dict] = []

    # ── Step 1: profile_table ──
    step_profile = _run_step(
        "profile_table",
        lambda: serialize_profile(profile_table(df, subject, level=level)),
    )
    steps.append(step_profile)
    profile_ctx = step_profile.get("result")

    # If profile failed, abort early (blocking failure)
    if step_profile["status"] == "error" or profile_ctx is None:
        return _build_workflow_result(
            "workflow_onboard", subject, steps, output_format=output_format,
        )

    # ── Step 2: suggest_rules ──
    step_suggest = _run_step(
        "suggest_rules",
        suggest_rules_context,
        profile_ctx=profile_ctx,
        output_format="dict",
    )
    steps.append(step_suggest)
    suggest_ctx = step_suggest.get("result")
    rules = suggest_ctx.get("rules", []) if suggest_ctx else []

    # ── Step 3: validate (adaptive — skip if 0 rules) ──
    validation_ctx: dict | None = None
    if not rules:
        steps.append({
            "tool": "validate",
            "status": "skipped",
            "duration_s": 0,
            "reason": "suggest_rules produced 0 rules",
            "result": None,
        })
    else:
        step_validate = _run_step(
            "validate",
            validation_summary_context,
            df,
            rules,
            subject=subject,
            output_format="dict",
        )
        steps.append(step_validate)
        validation_ctx = step_validate.get("result")

    # ── Step 4: microscope (adaptive — skip if 0 issues) ──
    # Determine worst columns: highest null_pct from profile
    column_profiles = profile_ctx.get("column_profiles", {})
    worst_columns: list[str] = []
    if isinstance(column_profiles, dict):
        scored = [
            (name, col.get("null_pct", 0))
            for name, col in column_profiles.items()
            if col.get("null_pct", 0) > 0.1 or col.get("quality_flags")
        ]
        scored.sort(key=lambda x: x[1], reverse=True)
        worst_columns = [name for name, _ in scored[:3]]

    # Also add columns with validation failures
    if validation_ctx:
        failed_rules = validation_ctx.get("rules", [])
        for rule in failed_rules:
            if not rule.get("passed", True):
                for col in rule.get("columns", []):
                    if col not in worst_columns and len(worst_columns) < 3:
                        worst_columns.append(col)

    if not worst_columns:
        steps.append({
            "tool": "microscope",
            "status": "skipped",
            "duration_s": 0,
            "reason": "no columns with significant issues found",
            "result": None,
        })
    else:
        for col_name in worst_columns[:3]:
            step_micro = _run_step(
                "microscope",
                lambda c=col_name: serialize_microscope(
                    microscope(df, c, subject=f"{subject}.{c}")
                ),
            )
            steps.append(step_micro)

    # ── Build result ──
    extra_metrics: dict[str, Any] = {
        "row_count": profile_ctx.get("metrics", {}).get("row_count"),
        "column_count": profile_ctx.get("metrics", {}).get("column_count"),
        "quality_score": profile_ctx.get("metrics", {}).get("quality_score"),
        "rules_suggested": len(rules),
    }
    if validation_ctx:
        extra_metrics["rules_failed"] = validation_ctx.get("metrics", {}).get(
            "rules_failed", 0
        )
        extra_metrics["is_promotion_safe"] = validation_ctx.get("metrics", {}).get(
            "is_promotion_safe"
        )

    # ── Annotate edge cases ──
    _all_null_cols = _detect_all_null_columns(df)
    if _all_null_cols:
        extra_metrics["all_null_columns"] = _all_null_cols
    if _sr_warn:
        extra_metrics["single_row"] = True

    return _build_workflow_result(
        "workflow_onboard",
        subject,
        steps,
        extra_metrics=extra_metrics,
        output_format=output_format,
    )


# ─── Reconcile Workflow ───────────────────────────────────────────────────────


def _reconcile_workflow(
    *args: Any,
    table: str | None = None,
    source: str | None = None,
    keys: list[str] | None = None,
    old_version: int | str | None = None,
    new_version: int | str | None = None,
    versions_ago: int = 1,
    watermark_col: str | None = None,
    subject: str | None = None,
    output_format: str = "dict",
    **kwargs: Any,
) -> dict | str:
    """Reconcile workflow — answers 'What CHANGED?'

    Sequence:
        delta_diff (or diff) -> partition_check -> watermark -> schema_diff

    Usage:
        anchor("reconcile", table="catalog.schema.table", keys=["id"])
        anchor("reconcile", old_df, new_df, keys=["id"])
        anchor("reconcile", table="catalog.schema.target", source="catalog.schema.source",
           keys=["id"], watermark_col="updated_at")

    Args:
        *args: Two positional DataFrames (old_df, new_df) for DF-mode.
        table: Delta table name for version-based reconciliation.
        source: Source table for watermark comparison.
        keys: Business key columns for row-level matching.
        old_version: Specific Delta version for old snapshot.
        new_version: Specific Delta version for new snapshot.
        versions_ago: Compare current vs N versions ago (default 1).
        watermark_col: Timestamp column for freshness check.
        subject: Human label.
        output_format: "dict" or "markdown".

    Returns:
        Workflow contract dict or markdown string.
    """
    from odibi_anchor.tables.diff_ops import diff_tables_by_key
    from odibi_anchor.tables.schema_diff_context import schema_diff_context

    # ── Resolve inputs ──
    old_df = None
    new_df = None

    if len(args) >= 2:
        # DataFrame-mode: two positional DFs
        old_df = args[0]
        new_df = args[1]
        if keys is None and len(args) >= 3:
            keys = args[2]
    elif table is not None:
        # Delta-mode: single table, version comparison
        pass
    elif len(args) == 1:
        # Single positional could be table name
        if isinstance(args[0], str):
            table = args[0]
        else:
            return build_base_context(
                kind="workflow_reconcile",
                subject="error",
                summary="Cannot reconcile a single DataFrame — provide two DFs or a Delta table name",
                metrics={},
                findings=["Usage: anchor('reconcile', old_df, new_df, keys=['id']) or anchor('reconcile', table='catalog.schema.table', keys=['id'])"],
            suggested_next_actions=[
                "Run anchor('help', 'reconcile') to see usage and parameters.",
                "Check that all required arguments are provided.",
            ],
            )
    else:
        return build_base_context(
            kind="workflow_reconcile",
            subject="error",
            summary="No input provided",
            metrics={},
            findings=["Usage: anchor('reconcile', old_df, new_df, keys=['id']) or anchor('reconcile', table='catalog.schema.table', keys=['id'])"],
            suggested_next_actions=[
                "Run anchor('help', 'reconcile') to see usage and parameters.",
                "Check that all required arguments are provided.",
            ],
        )

    if keys is None:
        return build_base_context(
            kind="workflow_reconcile",
            subject="error",
            summary="keys parameter is required",
            metrics={},
            findings=["Provide keys=[...] to define the business grain for comparison."],
            suggested_next_actions=[
                "Run anchor('help', 'reconcile') to see usage and parameters.",
                "Check that all required arguments are provided.",
            ],
        )
    # ── Empty input guard ──
    for _gdf in (old_df, new_df):
        guard = _guard_empty_df(_gdf, "reconcile")
        if guard is not None:
            return guard

    if isinstance(keys, str):
        keys = [keys]

    if subject is None:
        subject = table or "dataframe_comparison"

    steps: list[dict] = []

    # ── Step 1: diff or delta_diff ──
    diff_ctx: dict | None = None
    schema_changed = False

    if old_df is not None and new_df is not None:
        # DataFrame mode — use diff_tables_by_key
        step_diff = _run_step(
            "diff",
            diff_tables_by_key,
            old_df,
            new_df,
            keys=keys,
            output_format="dict",
        )
        steps.append(step_diff)
        diff_ctx = step_diff.get("result")
        if diff_ctx:
            metrics = diff_ctx.get("metrics", {})
            schema_changed = (
                metrics.get("old_only_column_count", 0) > 0
                or metrics.get("new_only_column_count", 0) > 0
                or metrics.get("type_changed_column_count", 0) > 0
            )
    else:
        # Delta table mode — use delta_diff_context
        try:
            from tools.delta_diff_tool.delta_diff_impl import delta_diff_context

            step_diff = _run_step(
                "delta_diff",
                delta_diff_context,
                table=table,
                keys=keys,
                old_version=old_version,
                new_version=new_version,
                versions_ago=versions_ago,
                output_format="dict",
            )
            steps.append(step_diff)
            diff_ctx = step_diff.get("result")
            if diff_ctx:
                schema_changed = diff_ctx.get("metrics", {}).get("schema_changed", False)
        except Exception as exc:
            steps.append({
                "tool": "delta_diff",
                "status": "error",
                "duration_s": 0,
                "error": str(exc),
                "summary": f"Import/setup error: {exc}",
                "result": None,
            })

    # If diff failed completely, return early
    if not diff_ctx and steps and steps[-1].get("status") == "error":
        return _build_workflow_result(
            "workflow_reconcile", subject, steps, output_format=output_format,
        )

    # ── Step 2: partition_check (adaptive — skip if not Delta table) ──
    if table is not None:
        try:
            from tools.partition_check_tool.partition_check_impl import (
                partition_check_context,
            )

            step_partition = _run_step(
                "partition_check",
                partition_check_context,
                table=table,
                output_format="dict",
            )
            steps.append(step_partition)
        except Exception as exc:
            steps.append({
                "tool": "partition_check",
                "status": "error",
                "duration_s": 0,
                "error": str(exc),
                "summary": f"partition_check unavailable: {exc}",
                "result": None,
            })
    else:
        steps.append({
            "tool": "partition_check",
            "status": "skipped",
            "duration_s": 0,
            "reason": "Not a Delta table — partition_check requires a table name",
            "result": None,
        })

    # ── Step 3: watermark (adaptive — skip if no source/watermark_col) ──
    if watermark_col and source:
        try:
            from tools.watermark_tool.watermark_impl import watermark_debug_context

            step_watermark = _run_step(
                "watermark",
                watermark_debug_context,
                source=source,
                target=table or new_df,
                watermark_col=watermark_col,
                output_format="dict",
            )
            steps.append(step_watermark)
        except Exception as exc:
            steps.append({
                "tool": "watermark",
                "status": "error",
                "duration_s": 0,
                "error": str(exc),
                "summary": f"watermark unavailable: {exc}",
                "result": None,
            })
    else:
        reason_parts = []
        if not source:
            reason_parts.append("no source provided")
        if not watermark_col:
            reason_parts.append("no watermark_col provided")
        steps.append({
            "tool": "watermark",
            "status": "skipped",
            "duration_s": 0,
            "reason": f"Watermark check skipped: {', '.join(reason_parts)}",
            "result": None,
        })

    # ── Step 4: schema_diff (adaptive — skip if no schema changes detected) ──
    if schema_changed and old_df is not None and new_df is not None:
        step_schema = _run_step(
            "schema_diff",
            schema_diff_context,
            old_df,
            new_df,
            output_format="dict",
        )
        steps.append(step_schema)
    else:
        reason = (
            "no schema changes detected in diff"
            if not schema_changed
            else "schema_diff requires two DataFrames (not available in Delta-only mode)"
        )
        steps.append({
            "tool": "schema_diff",
            "status": "skipped",
            "duration_s": 0,
            "reason": reason,
            "result": None,
        })

    # ── Build result ──
    extra_metrics: dict[str, Any] = {}
    if diff_ctx:
        diff_metrics = diff_ctx.get("metrics", {})
        if "added_key_count" in diff_metrics:
            # DataFrame diff mode
            extra_metrics["added_rows"] = diff_metrics.get("added_key_count", 0)
            extra_metrics["removed_rows"] = diff_metrics.get("removed_key_count", 0)
            extra_metrics["changed_rows"] = diff_metrics.get("changed_key_count", 0)
            extra_metrics["unchanged_rows"] = diff_metrics.get("unchanged_key_count", 0)
        elif "added_count" in diff_metrics:
            # Delta diff mode
            extra_metrics["added_rows"] = diff_metrics.get("added_count", 0)
            extra_metrics["removed_rows"] = diff_metrics.get("removed_count", 0)
            extra_metrics["changed_rows"] = diff_metrics.get("changed_count", 0)
            extra_metrics["unchanged_rows"] = diff_metrics.get("unchanged_count", 0)
        extra_metrics["schema_changed"] = schema_changed

    return _build_workflow_result(
        "workflow_reconcile",
        subject,
        steps,
        extra_metrics=extra_metrics,
        output_format=output_format,
    )


# ─── Investigate Workflow ──────────────────────────────────────────────────────


def _investigate_workflow(
    *args: Any,
    subject: str | None = None,
    columns: list[str] | str | None = None,
    rules: list[dict] | None = None,
    level: str = "standard",
    mode: str = "investigate",
    output_format: str = "dict",
    **kwargs: Any,
) -> dict | str:
    """Investigate workflow — answers 'What's WRONG?'

    Sequence:
        profile_table -> validate -> microscope (flagged cols) -> case_file -> coerce_check

    Adaptive logic:
        * suggest_rules runs only when explicit rules are not provided.
        * microscope examines explicit columns or columns flagged by profile/validate.
        * case_file runs for top null, outlier, or duplicate issues found upstream.
        * coerce_check runs only for pandas DataFrames when microscope finds likely
          whitespace, case, or format issues.
    """
    from odibi_anchor._dispatcher._tool_wrappers import (
        _case_file_context,
        _microscope_context,
        _profile_table_context,
    )
    from odibi_anchor.tables.coercion_classifier import coercion_check_context
    from odibi_anchor.validation.validation_summary_context import (
        validation_summary_context,
    )
    from tools.suggest_rules_tool.suggest_rules_impl import suggest_rules_context

    # ── Mode dispatch: onboard delegates to onboard workflow ──
    if mode == "onboard":
        return _onboard_workflow(*args, subject=subject, level=level, output_format=output_format, **kwargs)

    target = args[0] if args else None
    if target is None:
        return build_base_context(
            kind="workflow_investigate",
            subject="error",
            summary="No table or DataFrame provided",
            metrics={},
            findings=[
                "Usage: anchor('investigate', df, subject='orders') or anchor('investigate', 'catalog.schema.table', columns=['amount'])"
            ],
            suggested_next_actions=[
                "Run anchor('help', 'investigate') to see usage and parameters.",
                "Check that all required arguments are provided.",
            ],
        )

    if isinstance(columns, str):
        columns = [columns]

    explicit_rules = list(rules or [])

    if isinstance(target, str):
        try:
            from pyspark.sql import SparkSession

            spark = SparkSession.getActiveSession()
            df = spark.table(target)
        except Exception as exc:
            return build_base_context(
                kind="workflow_investigate",
                subject=target,
                summary=f"Failed to read table: {exc}",
                metrics={},
                findings=[f"spark.table('{target}') failed: {exc}"],
            )
        if subject is None:
            subject = target
    else:
        df = target
        if subject is None:
            subject = "dataframe"

    # ── Empty input guard ──
    guard = _guard_empty_df(df, "investigate")
    if guard is not None:
        return guard

    # ── Single-row and all-null warnings ──
    _sr_warn = _single_row_warning(df)

    steps: list[dict] = []

    step_profile = _run_step(
        "profile_table",
        _profile_table_context,
        df,
        subject=subject,
        level=level,
        output_format="dict",
    )
    steps.append(step_profile)
    profile_ctx = step_profile.get("result")

    if step_profile["status"] == "error" or profile_ctx is None:
        return _build_workflow_result(
            "workflow_investigate",
            subject,
            steps,
            output_format=output_format,
        )

    validation_rules = explicit_rules
    validation_ctx: dict | None = None

    if explicit_rules:
        steps.append(
            {
                "tool": "suggest_rules",
                "status": "skipped",
                "duration_s": 0,
                "reason": "explicit rules provided",
                "result": None,
            }
        )
    else:
        step_suggest = _run_step(
            "suggest_rules",
            suggest_rules_context,
            profile_ctx=profile_ctx,
            output_format="dict",
        )
        steps.append(step_suggest)
        suggest_ctx = step_suggest.get("result")
        validation_rules = suggest_ctx.get("rules", []) if suggest_ctx else []

    if not validation_rules:
        steps.append(
            {
                "tool": "validate",
                "status": "skipped",
                "duration_s": 0,
                "reason": "no validation rules available",
                "result": None,
            }
        )
    else:
        step_validate = _run_step(
            "validate",
            validation_summary_context,
            df,
            validation_rules,
            subject=subject,
            output_format="dict",
        )
        steps.append(step_validate)
        validation_ctx = step_validate.get("result")

    flagged_columns: list[str] = []
    if columns:
        flagged_columns.extend(columns)
    else:
        column_profiles = profile_ctx.get("column_profiles", {})
        if isinstance(column_profiles, dict):
            for col_name, col_profile in column_profiles.items():
                if col_profile.get("null_pct", 0) > 0.1 or col_profile.get(
                    "quality_flags"
                ):
                    flagged_columns.append(col_name)

        if validation_ctx:
            for rule in validation_ctx.get("rules", []):
                if rule.get("passed", True):
                    continue
                candidate_columns = rule.get("columns", [])
                if not candidate_columns and rule.get("column"):
                    candidate_columns = [rule["column"]]
                for col_name in candidate_columns:
                    if col_name:
                        flagged_columns.append(col_name)

    deduped_columns: list[str] = []
    seen_columns: set[str] = set()
    for col_name in flagged_columns:
        if col_name not in seen_columns:
            seen_columns.add(col_name)
            deduped_columns.append(col_name)
    flagged_columns = deduped_columns[:5]

    microscope_results: list[tuple[str, dict]] = []
    if not flagged_columns:
        steps.append(
            {
                "tool": "microscope",
                "status": "skipped",
                "duration_s": 0,
                "reason": "no flagged columns to inspect",
                "result": None,
            }
        )
    else:
        for col_name in flagged_columns:
            step_micro = _run_step(
                "microscope",
                _microscope_context,
                df,
                col_name,
                subject=f"{subject}.{col_name}",
                output_format="dict",
            )
            steps.append(step_micro)
            micro_ctx = step_micro.get("result")
            if step_micro["status"] == "ok" and micro_ctx is not None:
                microscope_results.append((col_name, micro_ctx))

    case_specs: list[dict[str, Any]] = []
    case_keys_seen: set[tuple[Any, ...]] = set()

    if validation_ctx:
        for rule in validation_ctx.get("rules", []):
            if rule.get("passed", True) or rule.get("rule_type") != "unique":
                continue
            key_columns = tuple(rule.get("columns", []))
            if key_columns and key_columns not in case_keys_seen and len(case_specs) < 3:
                case_keys_seen.add(key_columns)
                case_specs.append({"filter": "duplicates", "key_columns": list(key_columns)})

    for col_name, micro_ctx in microscope_results:
        metrics = micro_ctx.get("metrics", {})

        is_numeric = metrics.get("column_type_category") == "numeric"
        looks_like_identifier = "id" in col_name.lower() or col_name.lower().endswith("_key")
        if (
            metrics.get("outlier_count", 0) > 0
            and is_numeric
            and not looks_like_identifier
            and len(case_specs) < 3
        ):
            case_key = (col_name, "outliers")
            if case_key not in case_keys_seen:
                case_keys_seen.add(case_key)
                case_specs.append({"column": col_name, "filter": "outliers"})

        if metrics.get("null_count", 0) > 0 and len(case_specs) < 3:
            case_key = (col_name, "nulls")
            if case_key not in case_keys_seen:
                case_keys_seen.add(case_key)
                case_specs.append({"column": col_name, "filter": "nulls"})

    if not case_specs:
        steps.append(
            {
                "tool": "case_file",
                "status": "skipped",
                "duration_s": 0,
                "reason": "no null, outlier, or duplicate issues selected for row-level review",
                "result": None,
            }
        )
    else:
        for case_spec in case_specs[:3]:
            case_subject = case_spec.get("column") or "duplicates"
            step_case = _run_step(
                "case_file",
                _case_file_context,
                df,
                subject=f"{subject}.{case_subject}.{case_spec['filter']}",
                output_format="dict",
                **case_spec,
            )
            steps.append(step_case)

    coerce_columns: list[str] = []
    for col_name, micro_ctx in microscope_results:
        metrics = micro_ctx.get("metrics", {})
        findings_blob = " ".join(str(item) for item in micro_ctx.get("findings", [])).lower()
        has_case_mix = sum(
            1
            for metric_name in ("case_upper_pct", "case_lower_pct", "case_mixed_pct")
            if metrics.get(metric_name, 0) > 0
        ) >= 2
        if (
            metrics.get("column_type_category") == "string"
            and (
                metrics.get("whitespace_issues", 0) > 0
                or has_case_mix
                or "format" in findings_blob
                or "pattern" in findings_blob
            )
        ):
            coerce_columns.append(col_name)

    coerce_columns = list(dict.fromkeys(coerce_columns))[:5]

    if not coerce_columns:
        steps.append(
            {
                "tool": "coerce_check",
                "status": "skipped",
                "duration_s": 0,
                "reason": "microscope found no format, case, or whitespace issues",
                "result": None,
            }
        )
    elif detect_engine(df) != "pandas":
        steps.append(
            {
                "tool": "coerce_check",
                "status": "skipped",
                "duration_s": 0,
                "reason": "coerce_check currently supports pandas only",
                "result": None,
            }
        )
    else:
        import pandas as pd

        def _normalize_coercion_probe(value: Any) -> Any:
            if pd.isna(value):
                return value
            stripped = str(value).strip()
            parsed = pd.to_datetime(stripped, errors="coerce")
            if not pd.isna(parsed):
                return parsed.strftime("%Y-%m-%d")
            return stripped.upper() if stripped else stripped

        row_key = "__cw_rowid"
        old_df = df.reset_index(drop=True).copy()
        new_df = df.reset_index(drop=True).copy()
        old_df[row_key] = range(len(old_df))
        new_df[row_key] = range(len(new_df))
        for col_name in coerce_columns:
            new_df[col_name] = new_df[col_name].map(_normalize_coercion_probe)

        step_coerce = _run_step(
            "coerce_check",
            coercion_check_context,
            old_df,
            new_df,
            keys=[row_key],
            columns=coerce_columns,
            subject=f"{subject}_coercion",
            output_format="dict",
        )
        steps.append(step_coerce)

    profile_metrics = profile_ctx.get("metrics", {})
    extra_metrics: dict[str, Any] = {
        "row_count": profile_metrics.get("row_count"),
        "column_count": profile_metrics.get("column_count"),
        "quality_score": profile_metrics.get("quality_score"),
        "flagged_columns": len(flagged_columns),
        "columns_examined": len(flagged_columns),
        "case_files_run": sum(
            1
            for step in steps
            if step["tool"] == "case_file" and step["status"] == "ok"
        ),
    }
    if validation_ctx:
        extra_metrics["rules_evaluated"] = validation_ctx.get("metrics", {}).get(
            "rules_evaluated", 0
        )
        extra_metrics["rules_failed"] = validation_ctx.get("metrics", {}).get(
            "rules_failed", 0
        )
    if coerce_columns:
        extra_metrics["coercion_columns"] = len(coerce_columns)

    # ── Annotate edge cases ──
    _all_null_cols = _detect_all_null_columns(df)
    if _all_null_cols:
        extra_metrics["all_null_columns"] = _all_null_cols
    if _sr_warn:
        extra_metrics["single_row"] = True

    return _build_workflow_result(
        "workflow_investigate",
        subject,
        steps,
        extra_metrics=extra_metrics,
        output_format=output_format,
    )



def _debug_workflow(
    *args: Any,
    result_df: Any | None = None,
    upstreams: dict[str, Any] | None = None,
    keys: list[str] | str | None = None,
    filter_expr: str | None = None,
    target: Any | None = None,
    subject: str | None = None,
    output_format: str = "dict",
    **kwargs: Any,
) -> dict | str:
    """Debug workflow — answers 'WHY is it broken?'

    Sequence:
        diagnose_empty -> pre_join (problematic pairs) -> explain_row (if rows exist) -> pre_merge (if target)

    Adaptive logic:
        * diagnose_empty always runs to identify the dominant dropout stage.
        * pre_join runs only for upstream pairs that look problematic based on overlap diagnostics.
        * explain_row runs only when the result has rows and the workflow can identify sample key values.
        * pre_merge runs only when a target is provided for merge/upsert debugging.
    """
    from odibi_anchor._dispatcher._tool_wrappers import (
        _diagnose_empty_context,
        _explain_row_context,
        _pre_join_context,
        _pre_merge_context,
    )

    if result_df is None and args:
        result_df = args[0]
    if upstreams is None and len(args) >= 2:
        upstreams = args[1]

    if result_df is None:
        return build_base_context(
            kind="workflow_debug",
            subject="error",
            summary="No result DataFrame provided",
            metrics={},
            findings=[
                "Usage: anchor('debug', result_df, upstreams={'source': df1}, keys=['id'], filter_expr=\"status = 'Active'\")"
            ],
            suggested_next_actions=[
                "Run anchor('help', 'debug') to see usage and parameters.",
                "Check that all required arguments are provided.",
            ],
        )

    if not upstreams:
        return build_base_context(
            kind="workflow_debug",
            subject="error",
            summary="upstreams dict is required",
            metrics={},
            findings=[
                "Provide upstreams={'source': df1, 'dim': df2} so the workflow can diagnose where rows were lost."
            ],
            suggested_next_actions=[
                "Run anchor('help', 'debug') to see usage and parameters.",
                "Check that all required arguments are provided.",
            ],
        )

    if isinstance(keys, str):
        keys = [keys]
    keys = list(keys or [])

    if subject is None:
        subject = "result_df"

    steps: list[dict] = []

    step_diagnose = _run_step(
        "diagnose_empty",
        _diagnose_empty_context,
        result_df,
        upstreams,
        keys=keys,
        filter_expr=filter_expr,
        subject=subject,
        output_format="dict",
    )
    steps.append(step_diagnose)
    diagnose_ctx = step_diagnose.get("result")

    if step_diagnose["status"] == "error" or diagnose_ctx is None:
        return _build_workflow_result(
            "workflow_debug",
            subject,
            steps,
            output_format=output_format,
        )

    diagnose_metrics = diagnose_ctx.get("metrics", {})
    result_count = diagnose_metrics.get("result_count", 0)
    dropout_cause = diagnose_metrics.get("dropout_cause", "unknown")
    diagnose_samples = diagnose_ctx.get("samples", {})
    key_overlaps = diagnose_samples.get("key_overlaps", [])

    problematic_pairs: list[tuple[str, str]] = []
    for overlap in key_overlaps:
        pair = overlap.get("pair")
        if not isinstance(pair, str) or "↔" not in pair:
            continue
        left_name, right_name = pair.split("↔", 1)
        is_problematic = bool(overlap.get("error"))
        is_problematic = is_problematic or overlap.get("overlap_count", 1) == 0
        is_problematic = is_problematic or overlap.get("overlap_pct", 1.0) < 0.95
        if is_problematic and left_name in upstreams and right_name in upstreams:
            candidate = (left_name, right_name)
            if candidate not in problematic_pairs:
                problematic_pairs.append(candidate)

    if not problematic_pairs and dropout_cause in {"key_type_mismatch", "zero_key_overlap"} and len(upstreams) >= 2:
        upstream_names = list(upstreams.keys())
        for idx, left_name in enumerate(upstream_names):
            for right_name in upstream_names[idx + 1:]:
                problematic_pairs.append((left_name, right_name))

    if not problematic_pairs:
        steps.append(
            {
                "tool": "pre_join",
                "status": "skipped",
                "duration_s": 0,
                "reason": "diagnose_empty did not identify problematic upstream pairs",
                "result": None,
            }
        )
    else:
        for left_name, right_name in problematic_pairs[:3]:
            step_pre_join = _run_step(
                "pre_join",
                _pre_join_context,
                upstreams[left_name],
                upstreams[right_name],
                keys=keys,
                left_subject=left_name,
                right_subject=right_name,
                output_format="dict",
            )
            steps.append(step_pre_join)

    def _sample_key_values(df_obj: Any, key_columns: list[str]) -> dict[str, Any] | None:
        if not key_columns:
            return None
        engine = detect_engine(df_obj)
        if engine == "pandas":
            if df_obj.empty:
                return None
            row = df_obj.iloc[0]
            values = {col: row[col] for col in key_columns if col in df_obj.columns}
        elif engine == "spark":
            row = df_obj.select(*key_columns).limit(1).collect()
            if not row:
                return None
            values = {col: row[0][col] for col in key_columns if col in row[0].asDict()}
        else:
            return None
        return values if len(values) == len(key_columns) else None

    explain_values = _sample_key_values(result_df, keys) if result_count else None
    if result_count <= 0:
        steps.append(
            {
                "tool": "explain_row",
                "status": "skipped",
                "duration_s": 0,
                "reason": "result is truly empty — no row available to trace",
                "result": None,
            }
        )
    elif not keys:
        steps.append(
            {
                "tool": "explain_row",
                "status": "skipped",
                "duration_s": 0,
                "reason": "keys are required to trace a representative result row",
                "result": None,
            }
        )
    elif not explain_values:
        steps.append(
            {
                "tool": "explain_row",
                "status": "skipped",
                "duration_s": 0,
                "reason": "could not extract a representative row using the provided keys",
                "result": None,
            }
        )
    else:
        step_explain = _run_step(
            "explain_row",
            _explain_row_context,
            result_df,
            keys=keys,
            values=explain_values,
            upstream=upstreams,
            output_format="dict",
        )
        steps.append(step_explain)

    if target is None:
        steps.append(
            {
                "tool": "pre_merge",
                "status": "skipped",
                "duration_s": 0,
                "reason": "no target provided for merge debugging",
                "result": None,
            }
        )
    elif not keys:
        steps.append(
            {
                "tool": "pre_merge",
                "status": "skipped",
                "duration_s": 0,
                "reason": "keys are required to assess merge safety",
                "result": None,
            }
        )
    else:
        step_pre_merge = _run_step(
            "pre_merge",
            _pre_merge_context,
            result_df,
            target,
            keys=keys,
            source_subject=subject,
            target_subject=target if isinstance(target, str) else "target",
            output_format="dict",
        )
        steps.append(step_pre_merge)

    extra_metrics: dict[str, Any] = {
        "result_count": result_count,
        "upstream_count": len(upstreams),
        "problematic_pairs": len(problematic_pairs),
        "dropout_cause": dropout_cause,
    }

    return _build_workflow_result(
        "workflow_debug",
        subject,
        steps,
        extra_metrics=extra_metrics,
        extra_samples={
            "problematic_pairs": [f"{left}↔{right}" for left, right in problematic_pairs],
            "representative_values": explain_values or {},
        },
        output_format=output_format,
    )


# ─── Trace Workflow ───────────────────────────────────────────────────────────


def _trace_workflow(
    *args: Any,
    output_df: Any | None = None,
    keys: list[str] | str | None = None,
    values: dict[str, Any] | None = None,
    upstream: dict[str, Any] | None = None,
    subject: str | None = None,
    output_format: str = "dict",
    **kwargs: Any,
) -> dict | str:
    """Trace workflow — answers 'WHERE did this value come from?'

    Sequence:
        explain_row -> pre_join (if multiple upstreams) -> case_file (problematic upstreams)

    Adaptive logic:
        * explain_row always runs to trace the target row through upstream sources.
        * pre_join runs only when there are multiple upstreams (checks join integrity
          between each pair that contributed to the row).
        * case_file runs on upstreams where explain_row found missing (match_count=0)
          or duplicate (match_count>1) matches, investigating those keys.
    """
    from odibi_anchor._dispatcher._tool_wrappers import (
        _case_file_context,
        _explain_row_context,
        _pre_join_context,
    )

    # ── Resolve inputs ──
    if output_df is None and args:
        output_df = args[0]
    if keys is None and len(args) >= 2 and isinstance(args[1], list):
        keys = args[1]
    if values is None and len(args) >= 3 and isinstance(args[2], dict):
        values = args[2]
    if upstream is None and len(args) >= 4 and isinstance(args[3], dict):
        upstream = args[3]

    if output_df is None:
        return build_base_context(
            kind="workflow_trace",
            subject="error",
            summary="No output DataFrame provided",
            metrics={},
            findings=[
                "Usage: anchor('trace_row', output_df, keys=['id'], values={'id': 42}, "
                "upstream={'source': df1, 'dim': df2})"
            ],
            suggested_next_actions=[
                "Run anchor('help', 'trace') to see usage and parameters.",
                "Check that all required arguments are provided.",
            ],
        )

    if not keys:
        return build_base_context(
            kind="workflow_trace",
            subject="error",
            summary="keys parameter is required",
            metrics={},
            findings=[
                "Provide keys=[...] to identify the row to trace."
            ],
            suggested_next_actions=[
                "Run anchor('help', 'trace') to see usage and parameters.",
                "Check that all required arguments are provided.",
            ],
        )

    if not values:
        return build_base_context(
            kind="workflow_trace",
            subject="error",
            summary="values parameter is required",
            metrics={},
            findings=[
                "Provide values={'key_col': value} to identify the specific row to trace."
            ],
            suggested_next_actions=[
                "Run anchor('help', 'trace') to see usage and parameters.",
                "Check that all required arguments are provided.",
            ],
        )

    if not upstream:
        return build_base_context(
            kind="workflow_trace",
            subject="error",
            summary="upstream dict is required",
            metrics={},
            findings=[
                "Provide upstream={'source_name': df} so the workflow can trace "
                "where values originated."
            ],
            suggested_next_actions=[
                "Run anchor('help', 'trace') to see usage and parameters.",
                "Check that all required arguments are provided.",
            ],
        )

    # ── Empty input guard ──
    guard = _guard_empty_df(output_df, "trace_row")
    if guard is not None:
        return guard

    if isinstance(keys, str):
        keys = [keys]

    if subject is None:
        # Build subject from values
        val_parts = [f"{k}={v}" for k, v in list(values.items())[:3]]
        subject = ", ".join(val_parts)

    steps: list[dict] = []

    # ── Step 1: explain_row (always runs) ──
    step_explain = _run_step(
        "explain_row",
        _explain_row_context,
        output_df,
        keys=keys,
        values=values,
        upstream=upstream,
        output_format="dict",
    )
    steps.append(step_explain)
    explain_ctx = step_explain.get("result")

    # If explain_row failed, abort early (blocking failure)
    if step_explain["status"] == "error" or explain_ctx is None:
        return _build_workflow_result(
            "workflow_trace", subject, steps, output_format=output_format,
        )

    # ── Step 2: pre_join (adaptive — skip if only 1 upstream) ──
    upstream_names = list(upstream.keys())
    if len(upstream_names) < 2:
        steps.append({
            "tool": "pre_join",
            "status": "skipped",
            "duration_s": 0,
            "reason": "only 1 upstream — pre_join requires at least 2",
            "result": None,
        })
    else:
        # Run pre_join on consecutive upstream pairs
        for i in range(len(upstream_names) - 1):
            left_name = upstream_names[i]
            right_name = upstream_names[i + 1]
            step_pj = _run_step(
                "pre_join",
                _pre_join_context,
                upstream[left_name],
                upstream[right_name],
                keys=keys,
                left_subject=left_name,
                right_subject=right_name,
                output_format="dict",
            )
            steps.append(step_pj)

    # ── Step 3: case_file (adaptive — on problematic upstreams) ──
    # Identify upstreams with missing or duplicate matches from explain_row
    join_paths = explain_ctx.get("join_paths", [])
    problematic_upstreams: list[str] = []
    for path in join_paths:
        name = path.get("upstream", "")
        match_count = path.get("match_count", 1)
        if match_count == 0 or match_count > 1:
            if name in upstream:
                problematic_upstreams.append(name)

    if not problematic_upstreams:
        steps.append({
            "tool": "case_file",
            "status": "skipped",
            "duration_s": 0,
            "reason": "all upstreams matched cleanly (1:1)",
            "result": None,
        })
    else:
        for up_name in problematic_upstreams[:3]:
            # Investigate the key column in the problematic upstream
            key_col = keys[0] if keys else None
            if key_col is None:
                continue
            target_value = values.get(key_col)
            if target_value is not None:
                filter_expr = (
                    f"where:{key_col} == '{target_value}'"
                    if isinstance(target_value, str)
                    else f"where:{key_col} == {target_value}"
                )
            else:
                filter_expr = "nulls"

            step_cf = _run_step(
                "case_file",
                _case_file_context,
                upstream[up_name],
                column=key_col,
                filter=filter_expr,
                subject=f"{up_name}.{key_col}",
                output_format="dict",
            )
            steps.append(step_cf)

    # ── Build result ──
    explain_metrics = explain_ctx.get("metrics", {})
    extra_metrics: dict[str, Any] = {
        "columns_traced": explain_metrics.get("columns_traced", 0),
        "columns_untraced": explain_metrics.get("columns_untraced", 0),
        "upstream_count": len(upstream),
        "problematic_upstreams": len(problematic_upstreams),
        "exact_matches": explain_metrics.get("exact_matches", 0),
    }

    return _build_workflow_result(
        "workflow_trace",
        subject,
        steps,
        extra_metrics=extra_metrics,
        extra_samples={
            "join_paths": join_paths,
            "problematic_upstreams": problematic_upstreams,
            "traced_values": values,
        },
        output_format=output_format,
    )


# ─── Evolve Workflow ──────────────────────────────────────────────────────────


def _evolve_workflow(
    *args: Any,
    df: Any | None = None,
    target_schema: Any | None = None,
    coerce_ctx: dict | None = None,
    target_table: str | None = None,
    rules: list[dict] | None = None,
    case_target: str = "upper",
    dry_run: bool = True,
    subject: str | None = None,
    output_format: str = "dict",
    **kwargs: Any,
) -> dict | str:
    """Evolve workflow — answers 'What do I DO about it?'

    Sequence:
        schema_diff -> schema_migrate -> coerce_fix -> validate (confirm fix)

    Adaptive logic:
        * schema_diff runs only when target_schema is provided and coerce_ctx is not.
        * schema_migrate runs only if schema_diff found type changes.
        * coerce_fix runs only if coerce_ctx is provided or schema_diff found coercion issues.
        * validate runs only if coerce_fix or schema_migrate made changes.

    Usage:
        anchor("evolve", df, target_schema=target_df, subject="orders")
        anchor("evolve", df, coerce_ctx=coerce_check_result)
        anchor("evolve", df, target_schema=target_df, rules=[...])
    """
    from odibi_anchor.tables.schema_diff_context import schema_diff_context
    from odibi_anchor.validation.validation_summary_context import (
        validation_summary_context,
    )

    # ── Resolve inputs ──
    if df is None and args:
        df = args[0]
    if target_schema is None and len(args) >= 2:
        target_schema = args[1]

    if df is None:
        return build_base_context(
            kind="workflow_evolve",
            subject="error",
            summary="No DataFrame provided",
            metrics={},
            findings=[
                "Usage: anchor('evolve', df, target_schema=target_df) or "
                "anchor('evolve', df, coerce_ctx=coerce_check_result)"
            ],
            suggested_next_actions=[
                "Run anchor('help', 'evolve') to see usage and parameters.",
                "Check that all required arguments are provided.",
            ],
        )

    if target_schema is None and coerce_ctx is None:
        return build_base_context(
            kind="workflow_evolve",
            subject="error",
            summary="Either target_schema or coerce_ctx is required",
            metrics={},
            findings=[
                "Provide target_schema=target_df for schema evolution, or "
                "coerce_ctx=result from anchor('coerce_check') for data fixing."
            ],
            suggested_next_actions=[
                "Run anchor('help', 'evolve') to see usage and parameters.",
                "Check that all required arguments are provided.",
            ],
        )

    # ── Empty input guard ──
    guard = _guard_empty_df(df, "evolve")
    if guard is not None:
        return guard

    if subject is None:
        subject = "dataframe"

    steps: list[dict] = []
    schema_diff_result: dict | None = None
    has_type_changes = False
    coerce_needed = coerce_ctx is not None

    # ── Step 1: schema_diff (adaptive — skip if coerce_ctx provided directly) ──
    if coerce_ctx is not None:
        steps.append({
            "tool": "schema_diff",
            "status": "skipped",
            "duration_s": 0,
            "reason": "coerce_ctx provided directly — schema diff not needed",
            "result": None,
        })
    elif target_schema is not None:
        step_sd = _run_step(
            "schema_diff",
            schema_diff_context,
            df,
            target_schema,
            output_format="dict",
        )
        steps.append(step_sd)
        schema_diff_result = step_sd.get("result")
        if schema_diff_result:
            sd_metrics = schema_diff_result.get("metrics", {})
            has_type_changes = sd_metrics.get("type_changed_column_count", 0) > 0
            # If schema_diff found mismatches, flag coercion as needed
            coerce_needed = coerce_needed or has_type_changes
    else:
        steps.append({
            "tool": "schema_diff",
            "status": "skipped",
            "duration_s": 0,
            "reason": "no target_schema provided",
            "result": None,
        })

    # ── Step 2: schema_migrate (adaptive — skip if no type changes) ──
    migrate_result: dict | None = None
    if not has_type_changes:
        steps.append({
            "tool": "schema_migrate",
            "status": "skipped",
            "duration_s": 0,
            "reason": "no type changes detected in schema_diff",
            "result": None,
        })
    else:
        try:
            from tools.schema_migrate_tool.schema_migrate_impl import (
                schema_migrate_context,
            )

            step_migrate = _run_step(
                "schema_migrate",
                schema_migrate_context,
                schema_diff_ctx=schema_diff_result,
                target_table=target_table,
                dry_run=dry_run,
                output_format="dict",
            )
            steps.append(step_migrate)
            migrate_result = step_migrate.get("result")
        except Exception as exc:
            steps.append({
                "tool": "schema_migrate",
                "status": "error",
                "duration_s": 0,
                "error": str(exc),
                "summary": f"schema_migrate unavailable: {exc}",
                "result": None,
            })

    # ── Step 3: coerce_fix (adaptive — skip if no coercion issues) ──
    coerce_fix_result: dict | None = None
    if not coerce_needed:
        steps.append({
            "tool": "coerce_fix",
            "status": "skipped",
            "duration_s": 0,
            "reason": "no coercion issues identified",
            "result": None,
        })
    else:
        try:
            from tools.coerce_fix_tool.coerce_fix_impl import coerce_fix_context

            step_coerce = _run_step(
                "coerce_fix",
                coerce_fix_context,
                df=df,
                coerce_ctx=coerce_ctx,
                case_target=case_target,
                dry_run=dry_run,
                subject=subject,
                output_format="dict",
            )
            steps.append(step_coerce)
            coerce_fix_result = step_coerce.get("result")
        except Exception as exc:
            steps.append({
                "tool": "coerce_fix",
                "status": "error",
                "duration_s": 0,
                "error": str(exc),
                "summary": f"coerce_fix unavailable: {exc}",
                "result": None,
            })

    # ── Step 4: validate (adaptive — skip if no changes were made) ──
    changes_made = (
        (migrate_result is not None and migrate_result.get("metrics", {}).get("changes_applied", 0) > 0)
        or (coerce_fix_result is not None and coerce_fix_result.get("metrics", {}).get("columns_fixed", 0) > 0)
    )

    if not changes_made and not rules:
        steps.append({
            "tool": "validate",
            "status": "skipped",
            "duration_s": 0,
            "reason": "no changes applied — validation not needed",
            "result": None,
        })
    elif rules:
        # Use the fixed DataFrame from coerce_fix if available
        validate_df = coerce_fix_result.get("samples", {}).get("fixed_df", df) if coerce_fix_result else df
        step_validate = _run_step(
            "validate",
            validation_summary_context,
            validate_df,
            rules,
            subject=subject,
            output_format="dict",
        )
        steps.append(step_validate)
    else:
        steps.append({
            "tool": "validate",
            "status": "skipped",
            "duration_s": 0,
            "reason": "no validation rules provided to confirm fix",
            "result": None,
        })

    # ── Build result ──
    extra_metrics: dict[str, Any] = {
        "has_type_changes": has_type_changes,
        "coerce_needed": coerce_needed,
        "changes_made": changes_made,
        "dry_run": dry_run,
    }
    if schema_diff_result:
        sd_m = schema_diff_result.get("metrics", {})
        extra_metrics["type_changed_columns"] = sd_m.get("type_changed_column_count", 0)
        extra_metrics["added_columns"] = sd_m.get("new_only_column_count", 0)
        extra_metrics["removed_columns"] = sd_m.get("old_only_column_count", 0)
    if coerce_fix_result:
        cf_m = coerce_fix_result.get("metrics", {})
        extra_metrics["columns_fixed"] = cf_m.get("columns_fixed", 0)
        extra_metrics["rows_affected"] = cf_m.get("rows_affected", 0)

    return _build_workflow_result(
        "workflow_evolve",
        subject,
        steps,
        extra_metrics=extra_metrics,
        output_format=output_format,
    )
