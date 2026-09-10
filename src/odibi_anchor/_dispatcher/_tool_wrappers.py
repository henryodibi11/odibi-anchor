"""_tool_wrappers.py — Tool integration wrappers.

Extracted from agent_init.py Phase 1 (revamp spec).
Wrappers around tools/table_profiler_tool (profile_table, microscope, case_file).
"""


def _profile_table_context(*args, **kwargs):
    """Deep table profiling via tools/table_profiler_tool.

    Accepts either a fully-qualified table name (string) or a Spark/pandas DataFrame.
    Returns the Anchor standard output contract: {kind, subject, summary, metrics,
    findings, risks, samples, suggested_next_actions}.

    Usage:
        anchor("profile_table", "catalog.schema.table")
        anchor("profile_table", df, subject="my_table")
        anchor("profile_table", "catalog.schema.table", level="quick")
        anchor("profile_table", df, subject="t", output_format="markdown")
    """
    from tools.table_profiler_tool.lib.profiler import profile_table
    from tools.table_profiler_tool.lib.contract import serialize_profile
    from tools.table_profiler_tool.lib.renderer import render_table_profile_md, render_table_ai_summary

    # Parse arguments
    output_format = kwargs.pop("output_format", "dict")
    level = kwargs.pop("level", "standard")
    subject = kwargs.pop("subject", None)

    # First positional arg is either a table FQN (str) or a DataFrame
    if not args:
        return {
            "kind": "profile_table_context",
            "subject": "error",
            "summary": "No table or DataFrame provided",
            "metrics": {},
            "findings": ["Usage: anchor('profile_table', 'catalog.schema.table') or anchor('profile_table', df, subject='name')"],
            "risks": [],
            "samples": [],
            "suggested_next_actions": ["Provide a table name or DataFrame as first argument"],
        }

    target = args[0]

    if isinstance(target, str):
        # It's a fully-qualified table name — read it via spark
        try:
            spark = _get_spark_session()
            df = spark.table(target)
        except Exception as e:
            return {
                "kind": "profile_table_context",
                "subject": target,
                "summary": f"Failed to read table: {e}",
                "metrics": {},
                "findings": [f"spark.table('{target}') failed: {e}"],
                "risks": [],
                "samples": [],
                "suggested_next_actions": ["Verify table exists and you have SELECT permission"],
            }
        if subject is None:
            subject = target
    else:
        df = target
        if subject is None:
            subject = "dataframe"

    # ── Definitive empty-state (AXI #5): 0-row input cannot be profiled ──
    from odibi_anchor._utils.contract import (
        is_empty_df, empty_df_context, render_empty_df_md,
    )
    if is_empty_df(df):
        _ec = empty_df_context("profile_table_context", subject, "profile_table",
                               columns=list(getattr(df, "columns", [])))
        return render_empty_df_md(_ec) if output_format == "markdown" else _ec

    # Run profiler
    try:
        profile = profile_table(df, subject, level=level, **kwargs)
    except Exception as e:
        return {
            "kind": "profile_table_context",
            "subject": subject,
            "summary": f"Profiling failed: {e}",
            "metrics": {},
            "findings": [f"profile_table() raised: {type(e).__name__}: {e}"],
            "risks": [],
            "samples": [],
            "suggested_next_actions": ["Check error details, ensure table is readable"],
        }

    # Return in requested format
    if output_format == "markdown":
        return render_table_profile_md(profile)
    elif output_format == "ai_summary":
        return render_table_ai_summary(profile)
    else:
        return serialize_profile(profile)


def _microscope_context(*args, **kwargs):
    """Column-level deep-dive via tools/table_profiler_tool/lib/microscope.py.

    Investigates a single column: full distribution, pattern analysis,
    anomaly detection, value clustering, and actionable recommendations.

    Usage:
        anchor("microscope", df, "column_name")
        anchor("microscope", df, "column_name", subject="orders.customer_id")
        anchor("microscope", df, "column_name", sample_limit=30, bin_count=25)
        anchor("microscope", df, "column_name", output_format="markdown")
    """
    from tools.table_profiler_tool.lib.microscope import microscope
    from tools.table_profiler_tool.lib.contract import serialize_microscope
    from tools.table_profiler_tool.lib.renderer import render_microscope_md

    output_format = kwargs.pop("output_format", "dict")

    # Expect: anchor("microscope", df, "column_name", ...)
    if len(args) < 2:
        return {
            "kind": "microscope",
            "subject": "error",
            "summary": "Missing required arguments",
            "metrics": {},
            "findings": ["Usage: anchor('microscope', df, 'column_name', subject='...', sample_limit=20, bin_count=20)"],
            "risks": [],
            "samples": {},
            "suggested_next_actions": ["Provide a DataFrame and column name"],
        }

    df = args[0]
    column = args[1]

    # ── Definitive empty-state (AXI #5) ──
    from odibi_anchor._utils.contract import (
        is_empty_df, empty_df_context, render_empty_df_md,
    )
    if is_empty_df(df):
        _ec = empty_df_context("microscope", str(column), "microscope",
                               columns=list(getattr(df, "columns", [])))
        return render_empty_df_md(_ec) if output_format == "markdown" else _ec

    try:
        result = microscope(df, column, **kwargs)
    except Exception as e:
        return {
            "kind": "microscope",
            "subject": column,
            "summary": f"Microscope failed: {e}",
            "metrics": {},
            "findings": [f"microscope() raised: {type(e).__name__}: {e}"],
            "risks": [],
            "samples": {},
            "suggested_next_actions": ["Check column exists and DataFrame is valid"],
        }

    if output_format == "markdown":
        return render_microscope_md(result)
    return serialize_microscope(result)


def _case_file_context(*args, **kwargs):
    """Row-level investigation via tools/table_profiler_tool/lib/case_file.py.

    Investigates specific rows matching a condition. When the profile or
    microscope flags anomalies, the case file shows actual rows and context.

    Usage:
        anchor("case_file", df, column="customer_id", filter="nulls")
        anchor("case_file", df, column="amount", filter="outliers")
        anchor("case_file", df, filter="duplicates", key_columns=["id"])
        anchor("case_file", df, column="status", filter="top:5")
        anchor("case_file", df, filter="where:amount > 1000")
        anchor("case_file", df, row_ids=[101, 102], key_columns=["order_id"])
        anchor("case_file", df, column="x", filter="nulls", output_format="markdown")
    """
    from tools.table_profiler_tool.lib.case_file import case_file
    from tools.table_profiler_tool.lib.contract import serialize_case_file
    from tools.table_profiler_tool.lib.renderer import render_case_file_md

    output_format = kwargs.pop("output_format", "dict")

    # Expect: anchor("case_file", df, column="...", filter="...")
    if not args:
        return {
            "kind": "case_file",
            "subject": "error",
            "summary": "Missing required arguments",
            "metrics": {},
            "findings": ["Usage: anchor('case_file', df, column='col', filter='nulls|null_like|outliers|duplicates|top:N|bottom:N|where:EXPR')"],
            "risks": [],
            "samples": {},
            "suggested_next_actions": ["Provide a DataFrame and at least column or filter"],
        }

    df = args[0]

    # ── Definitive empty-state (AXI #5): distinguish empty input from "no matches" ──
    from odibi_anchor._utils.contract import (
        is_empty_df, empty_df_context, render_empty_df_md,
    )
    if is_empty_df(df):
        _ec = empty_df_context("case_file", str(kwargs.get("column", "rows")), "case_file",
                               columns=list(getattr(df, "columns", [])))
        return render_empty_df_md(_ec) if output_format == "markdown" else _ec

    try:
        result = case_file(df, **kwargs)
    except Exception as e:
        return {
            "kind": "case_file",
            "subject": kwargs.get("column", "unknown"),
            "summary": f"Case file failed: {e}",
            "metrics": {},
            "findings": [f"case_file() raised: {type(e).__name__}: {e}"],
            "risks": [],
            "samples": {},
            "suggested_next_actions": ["Check arguments — need column and/or filter"],
        }

    if output_format == "markdown":
        return render_case_file_md(result)
    return serialize_case_file(result)


def _diagnose_empty_context(*args, **kwargs):
    """Diagnose why a result DataFrame is empty or unexpectedly sparse."""
    from tools.diagnose_empty_tool.diagnose_empty_impl import diagnose_empty_context

    return diagnose_empty_context(*args, **kwargs)


def _pre_join_context(*args, **kwargs):
    """Assess join overlap, cardinality, nulls, and fanout risk before joining."""
    from tools.pre_join_tool.pre_join_impl import pre_join_context

    return pre_join_context(*args, **kwargs)


def _explain_row_context(*args, **kwargs):
    """Trace where a selected output row's values came from upstream sources."""
    from tools.explain_row_tool.explain_row_impl import explain_row_context

    return explain_row_context(*args, **kwargs)


def _pre_merge_context(*args, **kwargs):
    """Check whether a source DataFrame is safe to merge into a target."""
    from tools.pre_merge_tool.pre_merge_impl import pre_merge_context

    return pre_merge_context(*args, **kwargs)


def _get_spark_session():
    """Get the active SparkSession (runtime-only, lazy import)."""
    try:
        from pyspark.sql import SparkSession
        return SparkSession.getActiveSession()
    except ImportError:
        raise RuntimeError("PySpark not available — cannot read table by name")


# ─── Chain Context (cross-workflow chaining) ──────────────────────────────────


def _chain_context(
    *args,
    result: dict | None = None,
    target: str | None = None,
    subject: str | None = None,
    output_format: str = "dict",
    **kwargs,
) -> dict | str:
    """Extract chainable context from one workflow result to feed another.

    Supported chains:
        investigate → evolve: extracts coerce_check step result as coerce_ctx
        investigate(mode='onboard') → investigate: extracts profile step result for deeper investigation
        reconcile → debug: extracts diff context for debugging changes
        debug → trace_row: extracts explain_row result for deeper tracing

    Usage:
        investigate_result = anchor("investigate", df, subject="orders")
        chain_ctx = anchor("chain", result=investigate_result, target="evolve")
        # chain_ctx["kwargs"] can be spread into anchor("evolve", df, **chain_ctx["kwargs"])
    """
    from odibi_anchor._utils.contract import build_base_context

    # ── Resolve positional args ──
    if result is None and args:
        result = args[0]
    if target is None and len(args) >= 2:
        target = args[1]

    if result is None or not isinstance(result, dict):
        return build_base_context(
            kind="chain_context",
            subject="error",
            summary="No workflow result provided",
            metrics={},
            findings=[
                "Usage: anchor('chain', workflow_result, target='evolve')",
                "Pass the dict output from a workflow (e.g. anchor('investigate', ...)) as first arg.",
            ],
        )

    if target is None:
        return build_base_context(
            kind="chain_context",
            subject="error",
            summary="No target workflow specified",
            metrics={},
            findings=[
                "Usage: anchor('chain', result, target='evolve')",
                "Supported targets: evolve, investigate, debug, trace_row",
            ],
        )

    source_kind = result.get("kind", "")
    steps = result.get("steps", [])

    # ── Chain routing ──
    extracted: dict = {}
    chain_description = ""

    if target == "evolve":
        # investigate → evolve: extract coerce_check result
        for step in steps:
            if step.get("tool") == "coerce_check" and step.get("status") == "ok":
                extracted["coerce_ctx"] = step["result"]
                chain_description = "coerce_check → evolve.coerce_ctx"
                break
        if not extracted:
            # Try schema_diff from reconcile
            for step in steps:
                if step.get("tool") == "schema_diff" and step.get("status") == "ok":
                    extracted["target_schema_ctx"] = step["result"]
                    chain_description = "schema_diff → evolve context"
                    break

    elif target == "investigate":
        # investigate(mode='onboard') → investigate: extract profile and suggested rules
        for step in steps:
            if step.get("tool") == "profile_table" and step.get("status") == "ok":
                profile_result = step["result"]
                # Extract columns that need investigation (high-null, high-cardinality)
                cols = []
                col_profiles = profile_result.get("samples", {}).get("column_profiles", {})
                for col, prof in col_profiles.items() if isinstance(col_profiles, dict) else []:
                    null_rate = prof.get("null_rate", 0)
                    if null_rate > 0.1:
                        cols.append(col)
                extracted["columns"] = cols if cols else None
                extracted["subject"] = result.get("subject")
                chain_description = "profile → investigate.columns (high-null columns)"
                break

    elif target == "debug":
        # reconcile → debug: extract diff findings for debugging
        for step in steps:
            if step.get("tool") == "diff" and step.get("status") == "ok":
                diff_result = step["result"]
                extracted["filter_expr"] = None  # User must supply
                extracted["keys"] = diff_result.get("metrics", {}).get("keys", [])
                chain_description = "diff → debug.keys"
                break

    elif target == "trace_row":
        # debug → trace_row: extract explain_row result for deeper tracing
        for step in steps:
            if step.get("tool") == "explain_row" and step.get("status") == "ok":
                er_result = step["result"]
                join_paths = er_result.get("samples", {}).get("join_paths", [])
                # Find problematic upstreams
                problematic = [jp["upstream"] for jp in join_paths if jp.get("match_count", 1) != 1]
                extracted["problematic_upstreams"] = problematic
                extracted["keys"] = er_result.get("metrics", {}).get("keys", [])
                chain_description = "explain_row → trace_row context"
                break
    else:
        return build_base_context(
            kind="chain_context",
            subject="error",
            summary=f"Unsupported target workflow: '{target}'",
            metrics={},
            findings=[
                f"Target '{target}' is not a supported chain destination.",
                "Supported targets: evolve, investigate, debug, trace_row",
            ],
        )

    # ── Build output ──
    if not extracted:
        return build_base_context(
            kind="chain_context",
            subject=subject or source_kind,
            summary=f"No chainable context found for {source_kind} → {target}",
            metrics={"source_kind": source_kind, "target": target, "steps_scanned": len(steps)},
            findings=[
                f"Scanned {len(steps)} steps in source result but found no data to chain to '{target}'.",
                f"Source kind: {source_kind}",
                "The source workflow may not have produced the required step (e.g. coerce_check was skipped).",
            ],
        )

    return build_base_context(
        kind="chain_context",
        subject=subject or f"{source_kind}→{target}",
        summary=f"Extracted chain context: {chain_description}",
        metrics={
            "source_kind": source_kind,
            "target": target,
            "keys_extracted": list(extracted.keys()),
            "chain_description": chain_description,
        },
        findings=[
            f"Chain: {chain_description}",
            f"Use: anchor('{target}', df, **chain_result['samples']['kwargs'])",
        ],
        samples={"kwargs": extracted},
    )
