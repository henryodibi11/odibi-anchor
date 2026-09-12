"""Dispatch table metadata — signatures, groups, and help rendering.

This module contains the static configuration data for the anchor() dispatcher:
- DISPATCH_SIGS: action → usage signature string
- ACTION_GROUPS: category → list of actions (for grouped help)
- build_help_text(): renders the help output for anchor("help") and anchor("help", "action")

Extracted from agent_init.py Phase B of the Dispatcher Extraction spec.
The dispatch dict (action → callable) lives in init() as a closure by design —
it closes over ~20 session-state variables and is the natural product of init().
"""
from __future__ import annotations

import inspect
from typing import Any, Callable


# ─── Dispatch Signatures ─────────────────────────────────────────────────────
# Each entry maps an action name to its canonical usage string.
# Used by anchor("help") to display the API reference.

DISPATCH_SIGS: dict[str, str] = {
    "memory":       'anchor("memory", query="...")  # or recovery action=inspect|declare_abandoned|recover',
    "context":      'anchor("context", view="compact|summary|full")  # verified read-only agent context',
    "prepare":      'anchor("prepare", operation="task.create|problem.create|problem.update|work_item.create|work_item.update|gate.qualify|handoff.prepare|learning.assess", inputs={...})',
    "concurrency":  'anchor("concurrency", command="inspect|dry_run|apply|rollback", domains=[...])',
    "map":          'anchor("map", focus_file="path/to/file.py")',
    "impact":       'anchor("impact", target="file.py")',
    "consistency":  'anchor("consistency")',
    "convention":   'anchor("convention", action="new_function")',
    "safe":         'anchor("safe", target="file.py", action="append|replace", content="...")',
    "semantic":     'anchor("semantic", target="file.py", action="...", content="...")',
    "import_resolve": 'anchor("import_resolve", symbol="MyClass")',
    "known_bad":    'anchor("known_bad", changed_files=[...])',
    "task":         'anchor("task", "description", goal="...", mode="...", problem="PRB-...", spec="...", work_item="WI-YYYY-NNNN")',
    "gate":         'anchor("gate")  # no args — gate derives ALL evidence from session state',
    "preflight":    'anchor("preflight")  # auto-detects changed_files from session',
    "test":         'anchor("test")  # runs tests for changed files',
    "checkpoint":   'anchor("checkpoint", label="feature_name", learning_captures=[...], learning_assessment={"outcome": "..."})',
    "spec":         'anchor("spec")  # list | create | status | persist | from_problem | review | execute | done',
    "problem":      'anchor("problem")  # list | create | show | update | resume | link_spec | close',
    "work_item":    'anchor("work_item")  # list | create | show | update | preview | approve | record_publish | close',
    "incident_snapshot": 'anchor("incident_snapshot", collectors=[...], problem_id="PRB-...")',
    "environment_diff": 'anchor("environment_diff", left, right)',
    "spark_diagnose": 'anchor("spark_diagnose", plan_text, query_profile={...})',
    "uc_context": 'anchor("uc_context", metadata)',
    "delta_changes": 'anchor("delta_changes", detail, history=[...], cdf_summary={...})',
    "run_diff": 'anchor("run_diff", good_run, bad_run)',
    "observe_table": 'anchor("observe_table", profile, subject="catalog.schema.table", persist=False)',
    "table_trend": 'anchor("table_trend", observations)',
    "sync":         'UNAVAILABLE — external mutation requires a separate approved deployment process',
    "profile_table": 'anchor("profile_table", "catalog.schema.table")',
    "microscope":   'anchor("microscope", df, "column_name", subject="table.col", sample_limit=20, bin_count=20)',
    "case_file":    'anchor("case_file", df, column="col", filter="nulls|null_like|outliers|duplicates|top:N|bottom:N|where:EXPR")',
    "quality":      'anchor("quality", df, subject="table", keys=[...])',
    "validate":     'anchor("validate", df, rules=[...])',
    "duplicate":    'anchor("duplicate", df, ["key_col1", "key_col2"])',
    "diff":         'anchor("diff", old_df, new_df, keys=["id"])',
    "schema_diff":  'anchor("schema_diff", old_df, new_df)',
    "contract":     'anchor("contract", df, subject="t", candidate_key_columns=[...])',
    "transform":    'anchor("transform", profile_or_ctx, subject="table")',
    "apply_transform": 'anchor("apply_transform", df, plan_ctx, checkpoint=True)',
    "rollback":     'anchor("rollback", result, to="step_name")',
    "unpersist":    'anchor("unpersist", result)',
    "coerce_check": 'anchor("coerce_check", old_df, new_df, keys=["id"], columns=["col"])',
    "coerce_fix":  'anchor("coerce_fix", df, coerce_ctx, case_target="upper", dry_run=False)',
    "known_error":  'anchor("known_error", "traceback text")',
    "trace":        'anchor("trace", "error text")',
    "lookup":       'anchor("lookup", "symbol_or_story")',
    "learn":        'anchor("learn", ...)  # compatibility-only historical recovery when old persisted state technically requires it',
    "learning":     'anchor("learning", "capture|assess|safe_stop|list|show|insights|triage|export|backup", ...)',
    "save":         'anchor("save", entry_type="gotcha|convention|pattern|decision|discovery", content="...")',
    "confirm":      'anchor("confirm", "entry_id")  # blocked legacy compatibility; use governed memory promotion',
    "reject":       'anchor("reject", "entry_id")',
    "memory_stats": 'anchor("memory_stats")',
    "db_migrate":  'anchor("db_migrate")  # run all schema migrations on memory DB',
    "memory_hygiene": 'anchor("memory_hygiene")  # dry-run; apply exact reviewed IDs only',
    "memory_tags":  'anchor("memory_tags", tags=["tag1", "tag2"])',
    "session_files": 'anchor("session_files")',
    "session_diff": 'anchor("session_diff", target="file.py")  # or no target for all',
    "session_delta": 'anchor("session_delta")  # full session change summary',
    "review":       'anchor("review")  # pre-gate changeset review: diffstat + untested + criteria',
    "status":       'anchor("status")',
    "audit_history": 'anchor("audit_history")',
    "config":       'anchor("config")',
    "project":      'anchor("project")  # list | create | use | status | set_target | migrate',
    "skills":       'anchor("skills")',
    "references":   'anchor("references")  # list | match | load | search | load-section',
    "tools":        'anchor("tools")',
    "register_tool": 'anchor("register_tool", name="...", callable_path="module:func")',
    "frame":        'anchor("frame")',
    "touched":      'anchor("touched", "path/to/file.py", created=False)',
    "snapshot":     'anchor("snapshot", decisions=[...], next_steps=[...])',
    "save_snap":    'anchor("save_snap", snapshot, path)',
    "load_snap":    'anchor("load_snap", path)',
    "archive":      'anchor("archive", max_unused_days=90)  # compatibility: mark stale, never delete',
    "export_md":    'anchor("export_md")',
    "import_md":    'anchor("import_md")',
    "dogfood":      'anchor("dogfood", current_output)',
    "log":          'anchor("log", "category", "message")',
    "session_log":  'anchor("session_log")',
    "new_session":  'anchor("new_session", name="feature_name", features=3, inline=True)  # inline=False creates a notebook',
    "orient":       'anchor("orient")  # status + audit_history; task memory is deferred',
    "manifest":     'anchor("manifest")',
    "help":         'anchor("help")  # or anchor("help", "action_name") for details',
    # Registry tools (auto-discovered from tools/ directory)
    "pre_join":     'anchor("pre_join", left_df, right_df, keys=[...])',
    "pre_merge":    'anchor("pre_merge", source_df, target, keys=[...])',
    "diagnose_empty": 'anchor("diagnose_empty", result_df, upstreams={"source": df1, "dim": df2}, keys=[...], filter_expr="...")',
    "explain_row":    'anchor("explain_row", output_df, keys=["id"], values={"id": 42}, upstream={"source": df1, "dim": df2})',
    # ── Composed Workflows ──
    "reconcile":    'anchor("reconcile", table="catalog.schema.table", keys=["id"]) or anchor("reconcile", old_df, new_df, keys=["id"])',
    "investigate":  'anchor("investigate", "catalog.schema.table", columns=["amount"]) or anchor("investigate", df, subject="name", rules=[...])',
    "debug":        'anchor("debug", result_df, upstreams={"source": df1, "dim": df2}, keys=["id"], filter_expr="status = \'Active\'")',
    "trace_row":    'anchor("trace_row", output_df, keys=["id"], values={"id": 42}, upstream={"source": df1, "dim": df2})',
    "evolve":      'anchor("evolve", df, target_schema=target_df, subject="orders")',
    "chain":       'anchor("chain", workflow_result, target="evolve")',
}


# ─── Action Groups ────────────────────────────────────────────────────────────
# Categorized action lists for the grouped help overview.

ACTION_GROUPS: dict[str, list[str]] = {
    "Codebase & Memory": [
        "memory", "context", "map", "impact", "consistency", "convention",
        "safe", "semantic", "import_resolve", "known_bad",
    ],
    "Planning & Workflow": [
        "task", "prepare", "gate", "preflight",
        "test", "checkpoint", "spec", "problem", "work_item",
    ],
    "Data & Profiling": [
        "profile_table", "microscope", "case_file",
        "quality", "validate", "duplicate", "diff", "schema_diff",
        "contract", "transform", "apply_transform", "rollback", "unpersist",
        "coerce_check", "coerce_fix", "pre_join", "pre_merge", "diagnose_empty",
        "explain_row",
    ],
    "Operational Evidence": [
        "incident_snapshot", "environment_diff", "spark_diagnose", "uc_context",
        "delta_changes", "run_diff", "observe_table", "table_trend",
    ],
    "Debugging & Learning": [
        "known_error", "trace", "lookup", "learn", "learning", "save", "confirm", "reject",
    ],
    "Session & Snapshots": [
        "snapshot", "save_snap", "load_snap", "archive", "export_md", "import_md",
    ],
    "Diagnostics": [
        "concurrency", "memory_stats", "memory_tags", "db_migrate", "memory_hygiene", "session_files", "session_diff",
        "session_delta", "review", "status", "audit_history", "config",
        "project", "skills", "references", "tools", "register_tool", "frame", "manifest",
    ],
    "File Tracking": ["touched", "log", "session_log"],
    "Session Notebooks": ["new_session"],
    "Testing": ["dogfood"],
    "Composed Workflows": ["reconcile", "investigate", "debug", "trace_row", "evolve", "chain"],
}


_INTENT_MAP: dict[str, dict[str, str | list[str]]] = {
    # ── Token cost / output size ──
    "reduce token cost": {
        "use": ["map", "orient"],
        "tip": "Large reads accept output_format='toon' (compact) and some accept "
               "detail='summary'. e.g. anchor('map', detail='summary', output_format='toon'). "
               "anchor('orient') aggregates status+audit_history; task memory arrives after acceptance.",
    },
    # ── Pre-action safety checks ──
    "before a join": {
        "use": ["pre_join"],
        "tip": "Run BEFORE any join to catch null keys, cardinality mismatches, and coverage gaps.",
    },
    "before a merge": {
        "use": ["pre_merge"],
        "tip": "Run BEFORE MERGE INTO to catch duplicate keys and schema incompatibility.",
    },
    "before writing": {
        "use": ["quality", "gate"],
        "tip": "Run BEFORE writing a DataFrame to catch duplicates, nulls, and schema drift.",
    },
    "before editing code": {
        "use": ["map", "lookup", "skill_loaded"],
        "tip": "map to understand structure, lookup for correct signatures, skill_loaded to register skills.",
    },
    # ── Data exploration & profiling ──
    "new data": {
        "use": ["profile_table"],
        "tip": "profile_table for full stats (shape, nulls, cardinality, grain, freshness). Use level='quick' for a fast overview.",
    },
    "unknown table": {
        "use": ["profile_table", "investigate"],
        "tip": "profile_table for a quick look, investigate(mode='onboard') for full profiling + quality + contract.",
    },
    "profile a column": {
        "use": ["microscope"],
        "tip": "Deep column analysis: histograms, patterns, top values, outlier detection.",
    },
    "understand a table": {
        "use": ["profile_table", "contract"],
        "tip": "profile_table for full stats (shape, types, grain, freshness), contract for schema expectations.",
    },
    # ── Data quality & validation ──
    "validate data": {
        "use": ["quality", "validate"],
        "tip": "quality for automated checks (nulls, duplicates, anomalies), validate for custom rule lists.",
    },
    "duplicate keys": {
        "use": ["duplicate", "pre_join"],
        "tip": "duplicate for grain violations, pre_join also catches duplicates in join context.",
    },
    "null values": {
        "use": ["profile_table", "case_file", "quality"],
        "tip": "profile_table shows null counts, case_file isolates null rows, quality gates on null thresholds.",
    },
    "data quality": {
        "use": ["quality", "validate", "contract"],
        "tip": "quality for automated checks, validate for rule lists, contract for schema + key expectations.",
    },
    # ── Troubleshooting & debugging ──
    "something looks wrong": {
        "use": ["diagnose_empty", "known_error", "debug"],
        "tip": "diagnose_empty if 0 rows, known_error/trace for exceptions, debug for full investigation.",
    },
    "empty results": {
        "use": ["diagnose_empty", "debug"],
        "tip": "diagnose_empty traces why a query returned 0 rows through upstream DataFrames.",
    },
    "isolate bad rows": {
        "use": ["case_file"],
        "tip": "Filter by nulls, null_like, outliers, duplicates, top:N, bottom:N, or where:EXPR.",
    },
    "error in code": {
        "use": ["known_error", "trace"],
        "tip": "known_error for known-failure lookup, trace for structured root-cause extraction from tracebacks.",
    },
    "find a function": {
        "use": ["lookup", "import_resolve"],
        "tip": "lookup for function signatures/docs, import_resolve to find correct import paths.",
    },
    # ── Comparison & drift ──
    "compare tables": {
        "use": ["diff", "schema_diff", "reconcile"],
        "tip": "diff for row-level changes, schema_diff for column changes, reconcile for full audit.",
    },
    "data looks different today": {
        "use": ["diff", "reconcile", "schema_diff"],
        "tip": "diff for row changes, reconcile for full delta+watermark audit, schema_diff if columns shifted.",
    },
    "schema changes": {
        "use": ["schema_diff", "evolve"],
        "tip": "schema_diff to see what changed, evolve to auto-generate migration + coercion fixes.",
    },
    "values don't match": {
        "use": ["coerce_check", "coerce_fix"],
        "tip": "coerce_check classifies mismatches (case/whitespace/unicode/genuine), coerce_fix applies fixes.",
    },
    # ── Row-level investigation ──
    "trace a row": {
        "use": ["explain_row", "trace_row"],
        "tip": "explain_row for one row, trace_row for full upstream trace with pre_join + case_file.",
    },
    "why does this row look wrong": {
        "use": ["explain_row", "case_file", "microscope"],
        "tip": "explain_row traces upstream, case_file isolates similar rows, microscope profiles the column.",
    },
    # ── Data transformation ──
    "fix data quality": {
        "use": ["transform", "apply_transform", "rollback"],
        "tip": "transform generates a plan from profile output, apply_transform executes with rollback support.",
    },
    "clean data": {
        "use": ["transform", "apply_transform"],
        "tip": "transform auto-detects issues from profile output and generates a reviewable cleanup plan.",
    },
    "undo a transform": {
        "use": ["rollback", "unpersist"],
        "tip": "rollback reverts to a named step, unpersist frees Spark checkpoints from memory.",
    },
    # ── Bronze layer / ingestion ──
    "onboard a source": {
        "use": ["investigate"],
        "tip": "investigate(mode='onboard') runs the full chain: profile_table → quality → contract. Use for any new data source.",
    },
    "bronze troubleshooting": {
        "use": ["profile_table", "quality", "case_file"],
        "tip": "profile_table for shape+stats, quality for checks, case_file to isolate bad rows.",
    },
    "check a load": {
        "use": ["reconcile", "diff", "quality"],
        "tip": "reconcile for full delta+watermark audit, diff to compare versions, quality for post-load checks.",
    },
    # ── Planning & workflow ──
    "plan a task": {
        "use": ["task"],
        "tip": "Full readiness scoring with gaps and hints. Must reach 100% before writing code.",
    },
    "start working": {
        "use": ["orient", "task", "skill_loaded"],
        "tip": "orient, accept the task, review its bounded memory_context, then load required skills.",
    },
    "write a spec": {
        "use": ["spec"],
        "tip": 'anchor("task", mode="spec_creation") to plan, anchor("spec", "persist", result) to save.',
    },
    # ── Session management ──
    "check my session": {
        "use": ["status"],
        "tip": "Shows files changed, obligations pending, risk level, and action count.",
    },
    "what changed": {
        "use": ["session_diff", "session_delta", "session_files"],
        "tip": "session_diff for diffs, session_delta for summary, session_files for file list.",
    },
    "end of session": {
        "use": ["review", "gate", "checkpoint"],
        "tip": "review shows changeset, gate checks obligations, checkpoint saves progress with learnings.",
    },
    "save my work": {
        "use": ["checkpoint", "save", "snapshot"],
        "tip": "checkpoint for session progress, save for knowledge entries, snapshot for full state capture.",
    },
    "hand off work": {
        "use": ["snapshot"],
        "tip": "snapshot(mode='handoff') generates a transfer brief and captures session state for the next agent.",
    },
    # ── Code & codebase ──
    "understand the code": {
        "use": ["map", "impact", "lookup"],
        "tip": "map for codebase structure, impact for dependency analysis, lookup for function details.",
    },
    "edit code safely": {
        "use": ["safe", "semantic", "touched"],
        "tip": "safe for guardrailed edits (auto-chains checks), semantic for AST ops, touched to register changes.",
    },
    "check code quality": {
        "use": ["preflight", "test", "review"],
        "tip": "preflight for lint, test for pytest, review for pre-gate changeset review.",
    },
    # ── Memory & knowledge ──
    "remember something": {
        "use": ["learning"],
        "tip": "Capture only supported reusable observations; nothing_reusable_learned is valid.",
    },
    "find past knowledge": {
        "use": ["memory", "memory_tags", "memory_stats"],
        "tip": "Review bounded task memory first; query further only if insufficient, and reserve tag-wide scans for authorized governance audits.",
    },
}

# Keywords that map to intent keys for fuzzy matching
_INTENT_KEYWORDS: list[tuple[tuple[str, ...], str]] = [
    # Pre-action safety
    (("join", "joining", "left join", "inner join"), "before a join"),
    (("merge", "merging", "upsert", "merge into"), "before a merge"),
    (("write", "writing", "persist", "saving", "insert", "overwrite"), "before writing"),
    (("before edit", "before coding", "before changing"), "before editing code"),
    # Exploration & profiling
    (("new data", "unfamiliar", "first time", "never seen"), "new data"),
    (("unknown table", "catalog table", "what is this table"), "unknown table"),
    (("column", "histogram", "distribution", "column stats"), "profile a column"),
    (("understand", "what does this", "explain table"), "understand a table"),
    # Quality & validation
    (("validate", "rules", "check data"), "validate data"),
    (("duplicate", "grain", "key violation", "primary key"), "duplicate keys"),
    (("null", "missing", "empty column", "blank"), "null values"),
    (("data quality", "dq", "quality check"), "data quality"),
    # Troubleshooting
    (("wrong", "broken", "unexpected", "weird"), "something looks wrong"),
    (("empty", "zero rows", "no results", "nothing returned"), "empty results"),
    (("bad rows", "isolate", "filter rows", "outlier", "anomaly"), "isolate bad rows"),
    (("error", "fail", "exception", "traceback", "stack trace"), "error in code"),
    (("find function", "where is", "import", "signature"), "find a function"),
    # Comparison & drift
    (("compare", "diff", "before after", "side by side"), "compare tables"),
    (("different today", "changed since", "drift", "shifted"), "data looks different today"),
    (("schema", "ddl", "migration", "columns changed", "type change"), "schema changes"),
    (("mismatch", "don't match", "dont match", "coerce", "case sensitive", "whitespace"), "values don't match"),
    # Row investigation
    (("trace row", "lineage", "upstream", "where did", "origin"), "trace a row"),
    (("row looks wrong", "bad value", "investigate row"), "why does this row look wrong"),
    # Transformation
    (("fix quality", "clean up", "remediate"), "fix data quality"),
    (("clean", "transform", "normalize", "standardize"), "clean data"),
    (("undo", "revert", "rollback", "go back"), "undo a transform"),
    # Bronze / ingestion
    (("investigate", "new source", "ingest", "land"), "onboard a source"),
    (("bronze", "raw layer", "landing", "ingestion"), "bronze troubleshooting"),
    (("load", "etl", "pipeline output", "after load"), "check a load"),
    # Planning & workflow
    (("plan", "task", "ready", "start task"), "plan a task"),
    (("start", "begin", "kick off", "new task"), "start working"),
    (("spec", "specification", "design doc"), "write a spec"),
    # Session
    (("status", "session health", "where am i"), "check my session"),
    (("what changed", "my changes", "files changed", "session diff"), "what changed"),
    (("done", "finish", "end session", "wrap up"), "end of session"),
    (("save work", "checkpoint", "preserve"), "save my work"),
    (("snapshot", "hand off", "pass to", "transfer"), "hand off work"),
    # Code
    (("code structure", "codebase", "architecture"), "understand the code"),
    (("edit safely", "safe change", "guardrail"), "edit code safely"),
    (("lint", "test", "code review", "preflight"), "check code quality"),
    # Memory
    (("remember", "save knowledge", "store", "learned"), "remember something"),
    (("recall", "past knowledge", "search memory", "what did we learn"), "find past knowledge"),
]


# ─── Examples ─────────────────────────────────────────────────────────────────
# Concrete usage examples shown in anchor("help", "action_name") detail view.

_EXAMPLES: dict[str, str | list[str]] = {
    # Codebase & Memory
    "memory":       [
        'anchor("memory", query="how do we handle nulls")  # accepted task context was insufficient',
        'anchor("memory", "promotion", command="request_owner_activation", memory_id="...")',
        'anchor("memory", "promotion", command="request_owner_activation", memory_id="...", provider="databricks_in_session")',
        'anchor("memory", "promotion", command="request_owner_confirmation", memory_id="...")',
        'anchor("memory", "recovery", action="inspect")',
        'anchor("memory", "recovery", action="declare_abandoned", selection_id="...", prior_task_window_id="...", reason={...}, evidence={...})',
        'anchor("memory", "recovery", action="recover", selection_id="...", prior_task_window_id="...", reason={...}, evidence={...})',
    ],
    "map":          'anchor("map", focus_file="src/pipeline.py")',
    "impact":       'anchor("impact", target="src/utils/helpers.py")',
    "consistency":  'anchor("consistency")',
    "convention":   'anchor("convention", action="new_function", function_name="process_orders")',
    "safe":         'anchor("safe", target="src/etl.py", action="replace", function="load_orders", content="...")',
    "semantic":     'anchor("semantic", target="src/etl.py", action="add_import", content="from pathlib import Path")',
    "import_resolve": 'anchor("import_resolve", symbol="DeltaTable")',
    "known_bad":    'anchor("known_bad", changed_files=["src/etl.py", "src/utils.py"])',
    # Planning & Workflow
    "task":         'anchor("task", "fix null handling in bronze orders", goal="prevent null keys", mode="implementation")',
    "snapshot":     [
        'anchor("snapshot", mode="handoff", summary="bronze fix complete, silver layer next", state="in_progress", decisions=["used coalesce for nulls"])',
        'anchor("snapshot", decisions=["used SCD2 for dim_customer"], next_steps=["build fact_orders"])',
    ],
    "gate":         'anchor("gate")  # auto-checks all obligations',
    "preflight":    'anchor("preflight")  # auto-detects changed files',
    "test":         'anchor("test", target="tests/test_orders.py", mark="fast")',

    "checkpoint":   'anchor("checkpoint", label="bronze_fix", learning_captures=[...], learning_assessment={"outcome": "observations_recorded"})',
    "spec":         [
        'anchor("spec")  # list all specs',
        'anchor("spec", "persist", task_result)  # save task plan as spec',
        'anchor("spec", "review", "FEATURE_NAME")  # audit spec quality',
        'anchor("spec", "execute", "FEATURE_NAME")  # begin execution',
        'anchor("spec", "done", "FEATURE_NAME")  # mark complete',
    ],
    "problem":      [
        'anchor("problem", "create", title="Choose a queue scaling strategy")',
        'anchor("problem", "update", "PRB-2026-0001", hypothesis={"hypothesis": "Workers are undersized"})',
        'anchor("problem", "resume", "PRB-2026-0001")',
    ],
    "work_item":    [
        'anchor("work_item", "create", title="Ship queue scaling", outcome="Reduce processing delay")',
        'anchor("work_item", "preview", "WI-2026-0001", provider="asana", operations=["create_tasks"])',
        'anchor("work_item", "approve", "WI-2026-0001", provider="asana", operations=["create_tasks"], expected_fingerprint="sha256:...", approver="...", source="explicit user message")',
    ],
    # Data & Profiling
    "profile_table": 'anchor("profile_table", "catalog.bronze.sales_orders")',
    "microscope":   'anchor("microscope", orders_df, "order_amount", subject="orders.amount", bin_count=25)',
    "case_file":    [
        'anchor("case_file", df, column="customer_id", filter="nulls")',
        'anchor("case_file", df, column="amount", filter="outliers")',
        'anchor("case_file", df, column="status", filter="top:5")',
        'anchor("case_file", df, column="date", filter="where:date > \'2026-01-01\'")',
    ],
    "quality":      'anchor("quality", orders_df, subject="silver.orders", keys=["order_id"])',
    "validate":     'anchor("validate", df, rules=[{"type": "not_null", "columns": ["id", "date"]}, {"type": "unique", "columns": ["id"]}])',
    "duplicate":    'anchor("duplicate", orders_df, ["order_id", "order_date"])',
    "diff":         'anchor("diff", yesterday_df, today_df, keys=["order_id"])',
    "schema_diff":  'anchor("schema_diff", old_df, new_df)',
    "contract":     'anchor("contract", orders_df, subject="silver.orders", candidate_key_columns=["order_id"])',
    "transform":    'anchor("transform", profile_result, subject="bronze.orders")',
    "apply_transform": 'anchor("apply_transform", orders_df, plan_ctx, checkpoint=True)',
    "rollback":     [
        'anchor("rollback", result, to="dedup")  # rollback to named step',
        'anchor("rollback", result, to=3)  # rollback to step number',
    ],
    "unpersist":    'anchor("unpersist", transform_result)  # free Spark checkpoints',
    "coerce_check": 'anchor("coerce_check", source_df, target_df, keys=["id"], columns=["name", "status"])',
    "coerce_fix":   'anchor("coerce_fix", df, coerce_result, case_target="upper", dry_run=True)',
    "pre_join":     'anchor("pre_join", orders_df, customers_df, keys=["customer_id"])',
    "pre_merge":    'anchor("pre_merge", staging_df, "catalog.silver.orders", keys=["order_id"])',
    "diagnose_empty": 'anchor("diagnose_empty", result_df, upstreams={"orders": orders_df, "customers": cust_df}, keys=["id"])',
    "explain_row":  'anchor("explain_row", output_df, keys=["order_id"], values={"order_id": 42}, upstream={"orders": raw_df, "dim": dim_df})',
    # Debugging & Learning
    "known_error":  'anchor("known_error", "AnalysisException: Column \'order_id\' does not exist")',
    "trace":        'anchor("trace", traceback_text)',
    "lookup":       'anchor("lookup", "schema_diff_context")',
    "learn":        'anchor("learn", session_events=[...])  # compatibility-only historical recovery when old persisted state technically requires it',
    "learning":     [
        'anchor("learning", "capture", observation_type="reusable_practice", summary="...", signal_key="...")',
        'anchor("learning", "assess", outcome="observations_recorded", observation_ids=["obs_..."])',
        'anchor("learning", "triage", decision="derive_lesson", actor_kind="human", source_item_ids=["lrn_..."], expected_source_versions={"lrn_...": 1}, ...)',
        'anchor("learning", "safe_stop", status="blocked", reason="required evidence unavailable")',
    ],
    "save":         'anchor("save", entry_type="gotcha", content="Delta MERGE fails silently on null keys", tags=["delta", "merge"])',
    "confirm":      'anchor("confirm", "entry_abc123")  # blocked legacy compatibility; use governed memory promotion',
    "reject":       'anchor("reject", "entry_abc123")  # reject a pending memory entry',
    # Session & Snapshots
    "save_snap":    'anchor("save_snap", snapshot_result, "snapshots/bronze_fix.json")',
    "load_snap":    'anchor("load_snap", "snapshots/bronze_fix.json")',
    "archive":      'anchor("archive", max_unused_days=90)  # compatibility: mark stale, never delete',
    "export_md":    'anchor("export_md")  # export memory to markdown',
    "import_md":    'anchor("import_md")  # import memory from markdown',
    # Diagnostics
    "memory_stats": 'anchor("memory_stats")  # overview of memory DB',
    "memory_tags":  'anchor("memory_tags", tags=["bronze", "null-handling"])',
    "session_files": 'anchor("session_files")  # list all files touched this session',
    "session_diff": 'anchor("session_diff", target="src/etl.py")  # or no target for all changes',
    "session_delta": 'anchor("session_delta")  # full session change summary',
    "review":       'anchor("review")  # pre-gate changeset review: diffstat + untested + criteria',
    "status":       'anchor("status")  # zero-param health dashboard',
    "audit_history": 'anchor("audit_history")  # past gate audit results',
    "config":       'anchor("config")  # view/edit anti-pattern suppress settings',
    "project":      'anchor("project", "create", name="queue-automation")  # list/create/use/status managed projects',
    "skills":       'anchor("skills")  # list registered skills',
    "references":   'anchor("references", "search", "altair selection")  # search/load offline sections',
    "tools":        'anchor("tools")  # list registered tools',
    "register_tool": 'anchor("register_tool", name="my_checker", callable_path="my_module:check_func")',
    "frame":        'anchor("frame")  # show current context frame',
    "manifest":     'anchor("manifest")  # show boot manifest for drift detection',
    # File Tracking
    "touched":      [
        'anchor("touched", "src/etl.py")  # register file as changed',
        'anchor("touched", "src/new_module.py", created=True)  # register new file',
    ],
    "log":          'anchor("log", "pipeline", "bronze orders loaded 6454 rows")',
    "session_log":  'anchor("session_log")  # view session notes',
    # Session Notebooks
    "new_session":  'anchor("new_session", name="bronze_orders_fix", features=3)',
    # Testing
    "dogfood":      'anchor("dogfood", profile_result)  # self-test anchor output quality',
    # Composed Workflows
    "investigate":  [
        'anchor("investigate", "catalog.bronze.sales_orders", mode="onboard")  # by table name',
        'anchor("investigate", df, subject="sales_orders", mode="onboard")  # by DataFrame',
        'anchor("investigate", "catalog.bronze.orders", columns=["amount", "status"])',
        'anchor("investigate", df, subject="orders", rules=[{"type": "not_null", "columns": ["id"]}])',
    ],
    "reconcile":    [
        'anchor("reconcile", table="catalog.silver.orders", keys=["order_id"])  # Delta table',
        'anchor("reconcile", old_df, new_df, keys=["order_id"])  # two DataFrames',
    ],
    "debug":        'anchor("debug", result_df, upstreams={"orders": raw_df, "dim": dim_df}, keys=["id"], filter_expr="status = \'Active\'")',
    "trace_row":    'anchor("trace_row", output_df, keys=["order_id"], values={"order_id": 42}, upstream={"raw": raw_df, "dim": dim_df})',
    "evolve":       [
        'anchor("evolve", df, target_schema=target_df, subject="orders")  # from target schema',
        'anchor("evolve", df, coerce_ctx=coerce_result)  # from coerce_check output',
    ],
    "chain":        'anchor("chain", workflow_result, target="evolve")  # pipe one workflow into another',
    "help":         [
        'anchor("help")  # full API overview',
        'anchor("help", "profile_table")  # detailed help for one action',
        'anchor("help", "before a join")  # intent-based: what to use for a problem',
        'anchor("help", "workflow")  # common multi-step workflows',
    ],
    "sync":         'UNAVAILABLE — do not call; use a separately approved deployment process',
    "skill_loaded": 'anchor("skill_loaded", "skill-name")  # use each direct skill returned by the accepted task',
}


# ─── Workflows ────────────────────────────────────────────────────────────────
# Common multi-step patterns shown via anchor("help", "workflow").

_WORKFLOWS: dict[str, dict[str, str | list[str]]] = {
    "Bronze troubleshooting": {
        "when": "A bronze/raw table looks wrong or has unexpected data.",
        "steps": [
            'anchor("profile_table", "catalog.bronze.table")  # shape, nulls, types, full stats',
            'anchor("quality", df, subject="bronze.table", keys=["id"])  # automated checks',
            'anchor("case_file", df, column="problem_col", filter="nulls")  # isolate bad rows',
        ],
    },
    "Pre-join safety": {
        "when": "About to join two tables — check for issues first.",
        "steps": [
            'anchor("pre_join", left_df, right_df, keys=["id"])  # overlap, nulls, fanout',
            'anchor("coerce_check", left_df, right_df, keys=["id"], columns=["name"])  # value mismatches',
            'anchor("coerce_fix", df, coerce_result, dry_run=True)  # preview fixes',
            '# Now safe to join',
        ],
    },
    "New data source onboarding": {
        "when": "First time seeing a table or file — full investigation.",
        "steps": [
            'anchor("investigate", df, subject="source_name", mode="onboard")  # runs profile → quality → contract',
            '# Or step by step:',
            'anchor("profile_table", "catalog.schema.table")  # full stats',
            'anchor("quality", df, subject="source_name", keys=["id"])  # quality gate',
            'anchor("contract", df, subject="source_name", candidate_key_columns=["id"])  # schema contract',
        ],
    },
    "Compare table versions": {
        "when": "Something changed between loads — find out what.",
        "steps": [
            'anchor("schema_diff", old_df, new_df)  # column additions/removals/type changes',
            'anchor("diff", old_df, new_df, keys=["id"])  # row-level changes',
            'anchor("reconcile", old_df, new_df, keys=["id"])  # full audit: delta + watermark + partition + schema',
        ],
    },
    "Schema migration": {
        "when": "Source schema changed and you need to fix downstream.",
        "steps": [
            'anchor("schema_diff", old_df, new_df)  # see what changed',
            'anchor("evolve", df, target_schema=target_df, subject="table")  # auto-generate migration',
            '# evolve chains: schema_diff → schema_migrate → coerce_fix → validate',
        ],
    },
    "Data cleanup": {
        "when": "Profile showed issues — generate and apply a cleanup plan.",
        "steps": [
            'profile = anchor("profile_table", df, subject="table")  # identify issues',
            'plan = anchor("transform", profile, subject="table")  # generate cleanup plan',
            'result = anchor("apply_transform", df, plan, checkpoint=True)  # apply with rollback',
            'anchor("rollback", result, to="dedup")  # undo if needed',
            'anchor("unpersist", result)  # free Spark checkpoints when done',
        ],
    },
    "Row investigation": {
        "when": "A specific row has wrong values — trace it back to the source.",
        "steps": [
            'anchor("explain_row", output_df, keys=["id"], values={"id": 42}, upstream={"raw": raw_df})  # trace one row',
            'anchor("trace_row", output_df, keys=["id"], values={"id": 42}, upstream={"raw": raw_df, "dim": dim_df})  # full trace with pre_join + case_file',
            'anchor("microscope", df, "problem_column")  # deep-dive the column',
            'anchor("case_file", df, column="col", filter="where:id = 42")  # isolate the row',
        ],
    },
    "Debug empty results": {
        "when": "A query or join returned 0 rows — find out why.",
        "steps": [
            'anchor("diagnose_empty", result_df, upstreams={"src": src_df, "dim": dim_df}, keys=["id"])  # automated diagnosis',
            'anchor("debug", result_df, upstreams={"src": src_df, "dim": dim_df}, keys=["id"])  # full investigation workflow',
        ],
    },
    "Implementation task": {
        "when": "Starting a code change — full planning-to-completion flow.",
        "steps": [
            'anchor("status"); anchor("audit_history")  # orient and check prior evidence',
            'anchor("new_session", name="feature_name", inline=True)',
            'task_result = anchor("task", "description", goal="intended outcome", mode="implementation", acceptance_criteria=["completion check"])',
            '# Review task_result["memory_context"]; acknowledge bounded selections or none',
            '# Read and register every direct skill in task_result["required_skills"]',
            '# Persist and review a Spec only when the accepted task policy requires one',
            'anchor("known_bad", changed_files=[...])  # before Python edits',
            '# Edit, verify, and register every changed path with anchor("touched", "path")',
            'anchor("preflight")  # required for Python changes',
            'anchor("test")  # required for Python changes',
            'anchor("review")  # required for changed files',
            'anchor("gate")  # check all obligations before finishing',
            'anchor("learning", "assess", outcome="nothing_reusable_learned")  # or assess real Observation IDs',
        ],
    },
    "End of session": {
        "when": "Wrapping up work — save progress and hand off.",
        "steps": [
            'anchor("review")  # check changeset: diffstat + untested + criteria',
            'anchor("gate")  # verify all obligations met',
            'anchor("checkpoint", label="session_end", learning_captures=[...], learning_assessment={"outcome": "observations_recorded"})',
            'anchor("snapshot", mode="handoff", summary="work done", state="in_progress")  # if continuing later',
        ],
    },
}

_ACTION_DETAILS: dict[str, list[str]] = {
    "task": [
        '`memory_limit`: optional integer from 1 through 20 controlling how many ranked memories '
        'are selected at task acceptance (default 5). Every selection requires disposition.',
    ],
    "learning": [
        '`capture`: provide `observation_type` (`friction`, `blocker`, `near_miss`, '
        '`reusable_practice`, or `evidence_gap`), non-empty `summary` and `signal_key`; optional '
        '`impact` (`low`, `medium`, `high`, or `critical`, default `medium`), '
        '`applicability_scope` (`project_local`, `workbench`, or `cross_project`, default '
        '`workbench`), references, provenance, evidence, and `retry_latest=True`.',
        '`assess`: provide `outcome="observations_recorded"` with `observation_ids`, or '
        '`outcome="nothing_reusable_learned"` without them; optional `notes`, `actor_kind` '
        '(`agent` or `human`), `actor_ref`, and `retry_latest=True`. The result explains each '
        'semantic projection decision and supplies the next managed operation.',
        '`triage`: cross-project widening is human-only. Follow the assessed observation\'s '
        '`next_operation`, preserve its expected source version and evidence, and use '
        '`derive_lesson` or `derive_watch`; never create memory through SQLite.',
        '`safe_stop`: after the learning assessment has closed, terminate an accepted task with '
        'keyword-only `status` (`blocked` or `failed`) and a non-empty `reason`; optional '
        '`unavailable_evidence` is a list of strings. A successful gate cannot be safe-stopped.',
    ],
    "memory": [
        '`query`: use existing filters with `limit` and `offset` to page the final relevance-ranked '
        'matches. The response metrics include `total_matches`, `returned_count`, `offset`, '
        '`next_offset`, and `has_more`.',
        '`apply`: identify one task selection with `selection_id` or `memory_id`; '
        'provide non-empty `action` and optional JSON-object `context`.',
        '`disposition`: identify one pending selection with `selection_id` or `memory_id`; '
        'provide `disposition` (`applied`, `irrelevant`, `suspect`, or `superseded`) and a '
        'non-empty JSON-object `reason`. An `applied` disposition also records `action` and '
        'optional `context`. Use `all_pending=True` only with `disposition="irrelevant"`.',
        '`evaluate`: identify one unevaluated application with `application_id`, or the unique '
        'one for `memory_id`; provide `outcome` (`helpful`, `not_helpful`, `harmful`, or '
        '`superseded`) and a non-empty JSON-object `evidence`.',
        '`diagnostics`: inspect lifecycle counts and rates within the active project.',
        '`promotion`: use `command="inspect|evaluate|verify|attest|request_owner_activation|'
        'request_owner_confirmation|withdraw|quarantine"`. On Databricks, explicitly set '
        '`provider="databricks_in_session"` to choose the lower-assurance two-step challenge '
        'even when Slack is configured; omitted `provider` preserves the default provider order.',
        '`project="all"` means relevance-ranked eligibility across managed projects, not '
        'unconditional injection into every task. Owner promotion of such shared memories is '
        'bound to the boot-verified portfolio work authority.',
        'Lifecycle `reason`, `context`, and `evidence` values are JSON objects, not strings.',
        '**Lifecycle examples:**',
        '`anchor("memory", "apply", memory_id="...", action="used convention", '
        'context={"file": "src/job.py"})`',
        '`anchor("memory", "disposition", memory_id="...", disposition="irrelevant", '
        'reason={"reason": "not applicable"})`',
        '`anchor("memory", "evaluate", memory_id="...", outcome="helpful", '
        'evidence={"test": "passed"})`',
    ],
}


def build_help_text(
    target: str | None,
    action_funcs: dict[str, Callable[..., Any]] | None = None,
) -> str:
    """Build the help text for anchor("help") or anchor("help", "action_name").

    Supports four modes:
        anchor("help")                  -- grouped overview of all actions
        anchor("help", "action_name")   -- detailed signature + example for one action
        anchor("help", "before a join") -- intent-based lookup (what do I use for X?)
        anchor("help", "workflow")      -- common multi-step workflows

    Args:
        target: Action name, intent phrase, "workflow", or None for overview.
        action_funcs: Mapping of action->underlying callable for signature
            introspection. Pass None to skip signature details.

    Returns:
        Formatted help text string.
    """
    if not target:
        return _build_overview()
    if target == "workflow":
        return _build_workflows()
    # An exact action/tool name always gets its own detail page — check both the
    # documented signatures AND the introspectable callables (registered tools
    # like suggest_rules live only in action_funcs). This must precede intent
    # matching, else a real tool name fuzzy-matches an unrelated intent page.
    if target in DISPATCH_SIGS or (action_funcs and target in action_funcs):
        return _build_action_detail(target, action_funcs)
    intent_result = _match_intent(target)
    if intent_result:
        return intent_result
    return _build_action_detail(target, action_funcs)


def _build_action_detail(
    target: str,
    action_funcs: dict[str, Callable[..., Any]] | None,
) -> str:
    """Build detailed help for a single action."""
    has_sig = bool(action_funcs and target in action_funcs)
    if target not in DISPATCH_SIGS and not has_sig:
        return f"Unknown action: '{target}'. Run anchor(\"help\") for all actions."

    lines = [f'## anchor("{target}")\n']
    if target in DISPATCH_SIGS:
        lines.append(f"**Usage:** `{DISPATCH_SIGS[target]}`\n")

    # Try to get the underlying function's full signature
    if action_funcs and target in action_funcs:
        try:
            sig = inspect.signature(action_funcs[target])
            lines.append(
                f"**Full signature:** `{action_funcs[target].__name__}{sig}`\n"
            )
            # Extract parameter docs
            params = []
            for name, param in sig.parameters.items():
                if name in ("self", "root", "output_format"):
                    continue
                default = (
                    f" = {param.default!r}"
                    if param.default is not inspect.Parameter.empty
                    else ""
                )
                kind = (
                    "required"
                    if param.default is inspect.Parameter.empty
                    and param.kind
                    not in (param.VAR_POSITIONAL, param.VAR_KEYWORD)
                    else "optional"
                )
                params.append(f"  - `{name}`{default} ({kind})")
            if params:
                lines.append("**Parameters:**\n" + "\n".join(params))
        except (ValueError, TypeError):
            lines.append("*(signature not introspectable)*")

    if target in _ACTION_DETAILS:
        lines.append("\n**Subcommands:**\n")
        for detail in _ACTION_DETAILS[target]:
            lines.append(f"  - {detail}")

    # Add examples if available
    if target in _EXAMPLES:
        example = _EXAMPLES[target]
        if isinstance(example, list):
            lines.append("\n**Examples:**\n")
            for ex in example:
                lines.append(f"  - `{ex}`")
        else:
            lines.append(f"\n**Example:** `{example}`")

    return "\n".join(lines)


def _match_intent(query: str) -> str | None:
    """Match a free-text query against the intent map.

    Tries exact match first, then keyword matching.
    Returns formatted help text or None.
    """
    query_lower = query.lower().strip()

    # Exact intent match
    if query_lower in _INTENT_MAP:
        return _format_intent(query_lower, _INTENT_MAP[query_lower])

    # Keyword match -- collect all matching intents
    matches: list[str] = []
    for keywords, intent_key in _INTENT_KEYWORDS:
        if any(kw in query_lower for kw in keywords):
            if intent_key not in matches:
                matches.append(intent_key)

    if not matches:
        return None

    if len(matches) == 1:
        return _format_intent(matches[0], _INTENT_MAP[matches[0]])

    # Multiple matches -- show all
    lines = [f'# What to use for: "{query}"\n']
    for intent_key in matches:
        entry = _INTENT_MAP[intent_key]
        actions = entry["use"]
        lines.append(f"**{intent_key}**")
        for act in actions:
            sig = DISPATCH_SIGS.get(act, f'anchor("{act}")')
            lines.append(f"  - `{sig}`")
        lines.append(f"  {entry['tip']}\n")
    return "\n".join(lines)


def _format_intent(intent_key: str, entry: dict) -> str:
    """Format a single intent match as help text."""
    actions = entry["use"]
    lines = [f'# What to use for: "{intent_key}"\n']
    lines.append(f"**Tip:** {entry['tip']}\n")
    lines.append("**Actions:**\n")
    for act in actions:
        sig = DISPATCH_SIGS.get(act, f'anchor("{act}")')
        lines.append(f"  - `{sig}`")
    lines.append("")
    lines.append('Run `anchor("help", "action_name")` for full signature.')
    return "\n".join(lines)


def _build_overview() -> str:
    """Build the grouped overview of all actions."""
    lines = ["# anchor() API Reference\n"]
    lines.append('Tip: `anchor("help", "before a join")` -- ask what to use for a problem.')
    lines.append('     `anchor("help", "action_name")` -- detailed signature + example for one action.')
    lines.append('     `anchor("help", "workflow")` -- common multi-step workflows.\n')
    for group_name, actions in ACTION_GROUPS.items():
        lines.append(f"## {group_name}\n")
        for act in actions:
            sig = DISPATCH_SIGS.get(act, f'anchor("{act}")')
            lines.append(f"- `{sig}`")
        lines.append("")
    return "\n".join(lines)


def _build_workflows() -> str:
    """Build the workflow reference showing common multi-step patterns."""
    lines = ["# anchor() Common Workflows\n"]
    lines.append("Multi-step patterns for common data engineering tasks.\n")
    for name, workflow in _WORKFLOWS.items():
        lines.append(f"## {name}\n")
        lines.append(f"**When:** {workflow['when']}\n")
        lines.append("```python")
        for step in workflow["steps"]:
            lines.append(step)
        lines.append("```\n")
    return "\n".join(lines)
